"""The safe text-replacement engine (AGENTS.md §9, decisions D1/D5/D6).

Pipeline (never mutates the working document until validation passes):

  capability check -> font resolution (coverage-checked) -> candidate copy ->
  bounded char-aware redaction (fill=False, images/graphics preserved) ->
  insert replacement text -> validate (extraction, neighbors, pixels) ->
  commit (caller swaps in the candidate).

Mode A (PRESERVE_LINE): insert at the original baseline origin, same size and
color where possible; measure width and report collision instead of silently
overlapping.

Mode B (REFLOW_BOX): insert_textbox into an explicit rectangle (separate from
the source removal geometry); overflow is reported with a suggested size,
never auto-applied unless the edit opts in.

Validation splits the page into "changed" and "unchanged" using the union of
the source region box and the expected insertion extent, so legitimate edits
that are wider/taller than the source are not falsely rejected.

Coordinate spaces: region/line geometry (extraction) and all content-stream
calls (add_redact_annot, insert_text*) share PyMuPDF's unrotated, crop-normal
page space. Pixmaps render the rotated display space, so every rect fed to
pixel validation is first mapped through `page.rotation_matrix` (identity at
rotation 0).
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace

import pymupdf

from ...domain.models import (
    Color,
    Direction,
    EditCapability,
    EditMode,
    FontReference,
    Rect,
    ReplacementEdit,
    TextAlignment,
    TextRegion,
    ValidationResult,
)
from ..fonts.resolver import ResolvedFont, resolve_font
from .extractor import extract_page_lines
from .textnorm import (
    has_presentation_forms,
    needs_bidi,
    nfkc_offset_map,
    norm_cmp,
    strip_invisibles,
    to_logical,
)

_ALIGN_MAP = {
    TextAlignment.LEFT: pymupdf.TEXT_ALIGN_LEFT,
    TextAlignment.CENTER: pymupdf.TEXT_ALIGN_CENTER,
    TextAlignment.RIGHT: pymupdf.TEXT_ALIGN_RIGHT,
    TextAlignment.JUSTIFY: pymupdf.TEXT_ALIGN_JUSTIFY,
}

_PAD = 1.5  # pt inflation around the changed box for inside/outside splitting


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


def find_line_matches(
    text: str, needle: str, match_case: bool = False, whole_word: bool = False,
) -> list[tuple[int, int]]:
    """Char ranges of `needle` occurrences in one line's text (M10/M11).

    Same line granularity and left-to-right non-overlapping semantics as the
    worker's search. Whole-word mode guards against word characters on both
    sides, so "cat" never matches inside "category" — including when the
    needle itself starts or ends with punctuation.

    Matching happens in NFKC space (D20): extraction returns Arabic/Hebrew as
    presentation-form codepoints while users type logical characters, so both
    sides are folded before comparing and ranges are mapped back to exact
    indices of the ORIGINAL string. NFKC is identity for ordinary Latin text.
    """
    if not needle:
        return []
    hay, offsets = nfkc_offset_map(text)
    ned = unicodedata.normalize("NFKC", needle)
    if not match_case:
        hay = hay.lower()
        ned = ned.lower()
    raw: list[tuple[int, int]] = []
    if whole_word:
        pattern = r"(?<!\w)" + re.escape(ned) + r"(?!\w)"
        raw = [(m.start(), m.end()) for m in re.finditer(pattern, hay, 0)]
    else:
        start = 0
        while (pos := hay.find(ned, start)) != -1:
            raw.append((pos, pos + len(ned)))
            start = pos + max(1, len(ned))
    return [(offsets[s], offsets[e - 1] + 1) for s, e in raw]


def apply_ranges(text: str, ranges: list[tuple[int, int]], replacement: str) -> str:
    """Rebuild `text` with each [start, end) range swapped for `replacement` (M10).

    Ranges come from `find_line_matches` and are non-overlapping and sorted;
    sorting again keeps this safe for arbitrary subsets. One line with several
    matches becomes one substituted line in a single pass.
    """
    if not ranges:
        return text
    out: list[str] = []
    last = 0
    for start, end in sorted(ranges):
        out.append(text[last:start])
        out.append(replacement)
        last = end
    out.append(text[last:])
    return "".join(out)


def _to_mupdf_rect(r: Rect) -> pymupdf.Rect:
    return pymupdf.Rect(r.x0, r.y0, r.x1, r.y1)


def _union(a: Rect, b: Rect) -> Rect:
    return Rect(min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1))


def region_page_index(region: TextRegion) -> int:
    """Page index encoded in a region id (`<doc>:r<rev>:p<page>:k...`)."""
    part = region.region_id.split(":p", 1)[1]
    return int(part.split(":", 1)[0])


def _canonical_rtl_text(edit: ReplacementEdit) -> ReplacementEdit:
    """Bring extraction-space Arabic into logical space once (M11, D20).

    Producers store shaped Arabic word-reversed (visual order); the Story
    engine expects logical text and would double-reverse it. When the edit
    text carries presentation forms (marks extraction origin, possibly mixed
    with typed characters after a user edit), convert once here so shaping,
    validation and the save checks all see the same canonical text.
    """
    if has_presentation_forms(edit.new_text):
        return dc_replace(edit, new_text=to_logical(edit.new_text))
    return edit


@dataclass
class EditRecord:
    """Everything needed to replay one committed edit deterministically (D5)."""

    region: TextRegion | None  # None for pure insertions (Add Text)
    edit: ReplacementEdit
    resolved_family: str
    resolved_source: str
    page_index: int
    pre_revision: int
    post_revision: int
    redacted_text: str = ""  # text removed by a redaction commit (M9); feeds the
    # save-time forbidden-text check so removed text can never silently persist
    # M10 Replace All: all (region, edit) pairs of a batched page commit. Empty
    # for single edits — `region`/`edit` then describe the whole commit alone.
    pairs: list[tuple[TextRegion, ReplacementEdit]] = field(default_factory=list)


@dataclass
class PreparedEdit:
    """A validated candidate awaiting commit."""

    ok: bool
    candidate_bytes: bytes | None
    validation: ValidationResult
    preview_png: bytes | None
    record: EditRecord | None
    pre_bytes: bytes | None = None  # working doc snapshot for undo
    before_png: bytes | None = None  # pre-edit page render for the compare view
    issues: list[str] = field(default_factory=list)


@dataclass
class BatchPair:
    """One line replacement inside a Replace-All batch (M10).

    `match_ids` are the review-window match ids this pair folds (a line with
    several occurrences of the needle is one pair). Identity lives in the
    worker's batch, not here.
    """

    region: TextRegion
    edit: ReplacementEdit
    match_ids: list[int] = field(default_factory=list)


@dataclass
class PairOutcome:
    """Per-pair verdict of a batch prepare: planned or skipped with reason (M10)."""

    match_ids: list[int]
    status: str  # "planned" | "skipped"
    reason: str = ""  # skip reason, or shrink/substitution note when planned
    used_size: float | None = None
    font_family: str = ""
    font_substituted: bool = False


class PdfEditEngine:
    """Owns one working document; performs and validates edits serially.

    Lives in the worker process (D4). `revision` bumps on every committed edit.
    """

    def __init__(self, doc: pymupdf.Document, doc_id: str):
        self.doc = doc
        self.doc_id = doc_id
        self.revision = 0

    # -- capability -----------------------------------------------------------
    def capability(self, page_index: int, region: TextRegion) -> EditCapability:
        page = self.doc[page_index]
        for annot in page.annots() or []:
            if annot.type[0] == pymupdf.PDF_ANNOT_REDACT:
                return EditCapability(
                    editable=False,
                    reason="This page has an existing redaction annotation. Editing is "
                           "disabled on this page to avoid applying unrelated redactions.",
                )
        for line in region.lines:
            if line.direction != Direction.HORIZONTAL:
                return EditCapability(
                    editable=False,
                    reason="Vertical or rotated text cannot be edited yet.",
                )
            if not line.runs:
                return EditCapability(editable=False, reason="Region has no text runs.")
        if not region.lines:
            return EditCapability(editable=False, reason="Region is empty.")
        return EditCapability(editable=True, mode=region.mode)

    # -- helpers ---------------------------------------------------------------
    def _register_font(self, page: pymupdf.Page, resolved: ResolvedFont, tag: str) -> str:
        """Make the resolved font available on the page; return the resource name."""
        if resolved.builtin_name:
            return resolved.builtin_name
        name = f"OPS{tag}"
        kwargs: dict = {"fontname": name}
        if resolved.fontfile:
            kwargs["fontfile"] = resolved.fontfile
        elif resolved.fontbuffer:
            kwargs["fontbuffer"] = resolved.fontbuffer
        page.insert_font(**kwargs)
        return name

    def _measure(self, resolved: ResolvedFont, text: str, fontsize: float) -> float:
        if resolved.fontfile:
            f = pymupdf.Font(fontfile=resolved.fontfile)
        elif resolved.fontbuffer:
            f = pymupdf.Font(fontbuffer=resolved.fontbuffer)
        else:
            f = pymupdf.Font(fontname=resolved.builtin_name or "helv")
        return max(f.text_length(line, fontsize=fontsize) for line in text.split("\n"))

    def _right_neighbor_limit(self, page: pymupdf.Page, region: TextRegion) -> float:
        """x of the nearest text to the right on the same baseline, else page edge."""
        base_y = region.lines[0].baseline_y
        best = page.rect.width - 4.0
        for line in extract_page_lines(page, with_chars=False):
            if abs(line.baseline_y - base_y) > 2.0:
                continue
            if line.bbox.x0 > region.bbox.x1 - 0.5:
                best = min(best, line.bbox.x0 - 1.0)
        return best

    def _batch_right_limit(
        self, page: pymupdf.Page, region: TextRegion, other_regions: list[TextRegion],
    ) -> float:
        """Right-edge x for a batched Mode A insertion, from the ORIGINAL page (M10).

        Measured before any redaction so it is stable: the nearest line on the
        same baseline caps the width. A neighbour that is itself being replaced
        contributes its insertion origin (its new text starts exactly there),
        so two rebuilt cells of one table row can never overlap each other.
        """
        own = region.lines[0]
        base_y = own.baseline_y
        own_x = own.runs[0].origin.x
        # Only RIGHT-side replaced neighbours cap the width: a replaced cell to
        # the LEFT of this one does not shrink the space available to it (its
        # origin would even sit before ours, zeroing the gap — M10 fix after
        # real-world two-cells-per-baseline rows skipped every match).
        replaced_origins = {
            round(o.lines[0].runs[0].origin.x, 2)
            for o in other_regions
            if o is not region
            and abs(o.lines[0].baseline_y - base_y) <= 2.0
            and o.lines[0].runs[0].origin.x > own_x + 0.5
        }
        best = page.rect.width - 4.0
        for line in extract_page_lines(page, with_chars=False):
            if line.direction != Direction.HORIZONTAL:
                continue
            if abs(line.baseline_y - base_y) > 2.0:
                continue
            x = line.runs[0].origin.x
            if abs(x - own_x) < 0.5:
                continue  # the region's own line
            if round(x, 2) in replaced_origins:
                best = min(best, x - 1.0)
            elif line.bbox.x0 > own.bbox.x1 - 0.5:
                best = min(best, line.bbox.x0 - 1.0)
        return best

    def _fit_size(self, scratch_page: Callable[[], pymupdf.Page], box: Rect, text: str,
                  fontname: str, start: float, align: int, color) -> float:
        """Largest size <= start that fits `text` in `box` (binary search)."""
        lo, hi = 4.0, start
        best = 0.0
        for _ in range(14):
            mid = (lo + hi) / 2
            spare = scratch_page().insert_textbox(
                _to_mupdf_rect(box), text, fontsize=mid, fontname=fontname,
                align=align, color=color,
            )
            if spare >= 0:
                best = mid
                lo = mid
            else:
                hi = mid
            if hi - lo < 0.25:
                break
        return round(best * 2) / 2  # round down to half points

    def _add_region_redact_annots(self, page: pymupdf.Page, region: TextRegion) -> None:
        """Char-aware bounded redact annots for exactly the source text (D6).

        Consecutive char boxes are merged into segments to keep the annot count
        sane while staying tight enough that neighbors are not touched.
        `fill=False` means no paint-over: backgrounds survive. Callers apply
        once after adding annots for every region (batch path reuses this).
        """
        for line in region.lines:
            for run in line.runs:
                if run.char_bboxes:
                    seg = run.char_bboxes[0]
                    for cb in run.char_bboxes[1:]:
                        if abs(cb.y0 - seg.y0) < 0.5 and -0.5 <= cb.x0 - seg.x1 < 1.5:
                            seg = Rect(seg.x0, min(seg.y0, cb.y0), cb.x1, max(seg.y1, cb.y1))
                        else:
                            page.add_redact_annot(_to_mupdf_rect(seg), fill=False)
                            seg = cb
                    page.add_redact_annot(_to_mupdf_rect(seg), fill=False)
                else:
                    page.add_redact_annot(_to_mupdf_rect(run.bbox), fill=False)

    def _redact_region(self, page: pymupdf.Page, region: TextRegion) -> None:
        self._add_region_redact_annots(page, region)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )

    def _insert_mode_a(
        self, page: pymupdf.Page, region: TextRegion, edit: ReplacementEdit,
        resolved: ResolvedFont, fontname: str, size: float, color: Color,
        validation: ValidationResult, limit: float | None = None,
    ) -> tuple[float, Rect | None]:
        """Insert one line at the original baseline. Returns (used_size, extent).

        `limit` overrides the right-neighbour measurement (M10 batch path: the
        caller computes it from the ORIGINAL page, capping at a replaced
        neighbour's insertion origin so two rebuilt cells cannot overlap).
        """
        origin = region.lines[0].runs[0].origin
        text = _norm_ws(edit.new_text)
        width = self._measure(resolved, text, size)
        right = self._right_neighbor_limit(page, region) if limit is None else limit
        available = max(8.0, right - origin.x)
        used_size = size
        if width > available:
            validation.overflowed = True
            validation.overflow_px = width - available
            validation.required_size = max(4.0, round(size * available / width * 2) / 2)
            if edit.auto_shrink:
                used_size = validation.required_size
                width = self._measure(resolved, text, used_size)
                validation.overflowed = width > available
                validation.issues.append(
                    f"Text auto-shrunk to {used_size:g} pt to fit the line."
                )
            else:
                raise OverflowError(
                    "Replacement is wider than the available space and would "
                    "collide with neighboring text."
                )
        line = region.lines[0]
        page.insert_text(
            (origin.x, origin.y), text, fontsize=used_size,
            fontname=fontname, color=color.as_tuple(),
        )
        extent = Rect(origin.x, line.bbox.y0, origin.x + width, line.bbox.y1)
        return used_size, extent

    def _insert_mode_b(
        self, page: pymupdf.Page, region: TextRegion, edit: ReplacementEdit,
        resolved: ResolvedFont, fontname: str, size: float, color: Color,
        validation: ValidationResult, scratch_page: Callable[[], pymupdf.Page],
    ) -> tuple[float, Rect | None]:
        """Reflow text inside the target box. Returns (used_size, box)."""
        box = edit.target_box or region.paragraph_box or region.bbox
        align = _ALIGN_MAP[edit.alignment]
        spare = page.insert_textbox(
            _to_mupdf_rect(box), edit.new_text, fontsize=size,
            fontname=fontname, align=align, color=color.as_tuple(),
        )
        if spare < 0:
            validation.overflowed = True
            validation.overflow_px = -spare
            validation.required_size = self._fit_size(
                scratch_page, box, edit.new_text, fontname, size, align, color.as_tuple(),
            )
            if edit.auto_shrink and validation.required_size >= 4.0:
                raise _RetryWithSize(validation.required_size)
            raise OverflowError("Replacement text overflows the text box.")
        return size, box

    # -- shaped insertion (RTL / bidi scripts, M11+D20) ----------------------------
    _SHAPE_FONT_URL = "opsfont.ttf"

    def _shaped_css(self, resolved: ResolvedFont, size: float, color: Color) -> str:
        r, g, b = (round(c * 255) for c in color.as_tuple())
        if resolved.fontfile or resolved.fontbuffer:
            fam = "OPSFont"
            face = (f"@font-face {{font-family: {fam}; "
                    f"src: url({self._SHAPE_FONT_URL});}}\n")
        else:
            fam = "helvetica"
            face = ""
        return (f"{face}body {{font-family: {fam}; font-size: {size:g}pt; "
                f"line-height: 1.05; color: rgb({r}, {g}, {b}); margin: 0px;}}")

    def _shaped_archive(self, resolved: ResolvedFont) -> pymupdf.Archive | None:
        if resolved.fontfile:
            arch = pymupdf.Archive()
            arch.add(resolved.fontfile, self._SHAPE_FONT_URL)
            return arch
        if resolved.fontbuffer:
            arch = pymupdf.Archive()
            arch.add(resolved.fontbuffer, self._SHAPE_FONT_URL)
            return arch
        return None

    def _shaped_box(self, line_bbox: Rect, right_limit: float, size: float) -> Rect:
        """Layout box for a shaped (RTL) line: the original line's box grown
        to the same right-edge limit Mode A uses, so a longer replacement has
        room before it would collide with a neighbour (then it wraps and the
        vertical fit check rejects it honestly)."""
        return Rect(line_bbox.x0, line_bbox.y0,
                    max(line_bbox.x1, right_limit),
                    line_bbox.y1 + max(1.0, size * 0.2))

    def _shaped_width(
        self, resolved: ResolvedFont, text: str, size: float, color: Color,
    ) -> float:
        """Laid-out width of a shaped line, on a scratch page (M11/D20).

        text_length is meaningless for shaped bidi output, so the real Story
        layout measures it: huge box, single line, read the line bbox.
        """
        scratch = pymupdf.open()
        try:
            sp = scratch.new_page(width=1000, height=100)
            sp.insert_htmlbox(
                pymupdf.Rect(10, 10, 990, 90),
                f"<div>{html.escape(strip_invisibles(text))}</div>",
                css=self._shaped_css(resolved, size, color),
                archive=self._shaped_archive(resolved), scale_low=1)
            lines = [ln for b in sp.get_text("dict")["blocks"] if b.get("type") == 0
                     for ln in b["lines"]]
            if not lines:
                return 0.0
            x0 = min(ln["bbox"][0] for ln in lines)
            x1 = max(ln["bbox"][2] for ln in lines)
            return x1 - x0
        finally:
            scratch.close()

    def _fit_shaped_size(
        self, resolved: ResolvedFont, text: str, start: float, color: Color,
        available: float,
    ) -> float | None:
        """Largest size <= start (half-point steps) whose shaped width fits."""
        lo, hi = 4.0, start
        best = None
        for _ in range(10):
            mid = (lo + hi) / 2
            if self._shaped_width(resolved, text, mid, color) <= available:
                best = round(mid * 2) / 2
                lo = mid
            else:
                hi = mid
            if hi - lo < 0.25:
                break
        return best

    def _insert_shaped_line(
        self, page: pymupdf.Page, region: TextRegion, edit: ReplacementEdit,
        resolved: ResolvedFont, size: float, color: Color, right_limit: float,
    ) -> Rect:
        """Insert an RTL/bidi line via the Story engine (M11, D20).

        MuPDF shapes with HarfBuzz and applies its own bidi, producing real,
        extractable PDF text (probe: scripts/probe_rtl.py). The text anchors
        to the line's BOX — an RTL line preserves its box, not the exact
        baseline (documented deviation). `scale_low=1` refuses to shrink
        silently; no fit raises OverflowError like Mode A overflow.
        """
        box = self._shaped_box(region.lines[0].bbox, right_limit, size)
        # strip invisible formatting first: Story treats soft hyphens as
        # hyphenation points and drops them, which would change the text
        markup = f"<div>{html.escape(strip_invisibles(edit.new_text))}</div>"
        spare, _scale = page.insert_htmlbox(
            _to_mupdf_rect(box), markup, css=self._shaped_css(resolved, size, color),
            archive=self._shaped_archive(resolved), scale_low=1)
        if spare < 0:
            raise OverflowError("Replacement text does not fit the line box.")
        return box

    # -- prepare ---------------------------------------------------------------
    def prepare(
        self,
        page_index: int,
        region: TextRegion,
        edit: ReplacementEdit,
        preview_zoom: float = 2.0,
    ) -> PreparedEdit:
        def fail(validation: ValidationResult, issues: list[str]) -> PreparedEdit:
            validation.ok = False
            validation.issues.extend(issues)
            reported = list(dict.fromkeys(validation.issues))
            return PreparedEdit(ok=False, candidate_bytes=None, validation=validation,
                                preview_png=None, record=None, issues=reported)

        edit = _canonical_rtl_text(edit)
        if region.revision != self.revision or edit.source_revision != self.revision:
            return fail(ValidationResult(ok=False), ["Stale region: the document changed."])
        cap = self.capability(page_index, region)
        if not cap.editable:
            return fail(ValidationResult(ok=False), [cap.reason])

        if edit.mode != EditMode.PRESERVE_LINE:
            # Mode B: the target box must not sit on top of foreign text (§9)
            target = edit.target_box or region.paragraph_box or region.bbox
            probe = (target.inflated(-1.0, -1.0)
                     if target.width > 6 and target.height > 6 else target)
            own = region.bbox.inflated(1.0, 1.0)
            for ln in extract_page_lines(self.doc[page_index], with_chars=False):
                if ln.bbox.intersects(own):
                    continue  # own lines are removed first
                if ln.bbox.intersects(probe):
                    return fail(ValidationResult(ok=False),
                                ["The text box overlaps other text. "
                                 "Resize or move the box first."])

        dom = region.dominant_font
        size = edit.size_override or dom.size
        color: Color = edit.color_override or dom.color
        resolved = resolve_font(
            self.doc, dom, set(edit.new_text),
            bold=edit.bold_override, italic=edit.italic_override,
            user_choice=edit.font_override,
        )

        validation = ValidationResult(ok=True)
        if resolved.substituted:
            validation.substituted_font = resolved.family
        if resolved.missing_glyphs:
            validation.missing_glyphs = resolved.missing_glyphs
            return fail(validation, [
                f"Font {resolved.family} cannot render: "
                f"{''.join(resolved.missing_glyphs[:10])}"
            ])

        pre_bytes = self.doc.tobytes()
        candidate = pymupdf.open(stream=pre_bytes)
        try:
            used_size = size
            change_box: Rect | None = None
            for attempt in range(2):  # second pass only after _RetryWithSize
                if attempt:
                    candidate.close()
                    candidate = pymupdf.open(stream=pre_bytes)
                page = candidate[page_index]
                self._redact_region(page, region)
                fontname = self._register_font(page, resolved, f"E{page_index}A{attempt}")

                def _scratch(_page=page, _fontname=fontname, _resolved=resolved) -> pymupdf.Page:
                    d = pymupdf.open()
                    p = d.new_page(width=_page.rect.width, height=_page.rect.height)
                    if _resolved.fontfile:
                        p.insert_font(fontname=_fontname, fontfile=_resolved.fontfile)
                    elif _resolved.fontbuffer:
                        p.insert_font(fontname=_fontname, fontbuffer=_resolved.fontbuffer)
                    return p

                try:
                    if edit.mode == EditMode.PRESERVE_LINE:
                        if needs_bidi(edit.new_text):
                            # shaped path: measured width against the same
                            # right-edge limit as Mode A (§9 overflow choices,
                            # shrink only on explicit opt-in like Mode A)
                            limit = self._right_neighbor_limit(page, region)
                            available = max(8.0, limit - region.lines[0].bbox.x0)
                            width = self._shaped_width(resolved, edit.new_text,
                                                       size, color)
                            used_size = size
                            if width > available:
                                required = self._fit_shaped_size(
                                    resolved, edit.new_text, size, color, available)
                                validation.overflowed = True
                                validation.overflow_px = width - available
                                validation.required_size = required
                                if edit.auto_shrink and required:
                                    used_size = required
                                    width = self._shaped_width(
                                        resolved, edit.new_text, used_size, color)
                                    validation.overflowed = width > available
                                    validation.issues.append(
                                        f"Text auto-shrunk to {used_size:g} pt to "
                                        f"fit the line.")
                                else:
                                    raise OverflowError(
                                        "Replacement is wider than the available "
                                        "space and would collide with neighboring "
                                        "text.")
                            extent = self._insert_shaped_line(
                                page, region, edit, resolved, used_size, color, limit)
                        else:
                            used_size, extent = self._insert_mode_a(
                                page, region, edit, resolved, fontname, size, color,
                                validation)
                    else:
                        used_size, extent = self._insert_mode_b(
                            page, region, edit, resolved, fontname, size, color,
                            validation, _scratch)
                except _RetryWithSize as retry:
                    size = retry.size
                    validation.overflowed = False
                    validation.overflow_px = 0.0
                    validation.issues.append(
                        f"Text auto-shrunk to {retry.size:g} pt to fit the box."
                    )
                    continue
                change_box = _union(region.bbox, extent) if extent else region.bbox
                break
            else:  # pragma: no cover - loop always breaks or raises
                raise RuntimeError("insertion attempts exhausted")

            padded = change_box.inflated(_PAD, _PAD)
            self._validate_extraction(candidate[page_index], page_index, region, edit,
                                      padded, validation)
            before_png = None
            after = None
            if validation.ok:
                before = self.doc[page_index].get_pixmap(
                    matrix=pymupdf.Matrix(preview_zoom, preview_zoom))
                after = candidate[page_index].get_pixmap(
                    matrix=pymupdf.Matrix(preview_zoom, preview_zoom))
                self._validate_pixels(before, after, self._display_rect(page_index, padded),
                                      preview_zoom, validation)
                before_png = before.tobytes("png")

            if not validation.ok:
                candidate.close()
                return fail(validation, [])

            preview_png = after.tobytes("png")
            cand_bytes = candidate.tobytes(deflate=True)
            candidate.close()
        except OverflowError as exc:
            try:
                candidate.close()
            except Exception:
                pass
            return fail(validation, [str(exc)])
        except Exception as exc:  # engine failure must never corrupt the working doc
            try:
                candidate.close()
            except Exception:
                pass
            return fail(ValidationResult(ok=False), [f"Edit failed: {exc}"])

        record = EditRecord(
            region=region, edit=edit, resolved_family=resolved.family,
            resolved_source=resolved.source, page_index=page_index,
            pre_revision=self.revision, post_revision=self.revision + 1,
        )
        return PreparedEdit(
            ok=True, candidate_bytes=cand_bytes, validation=validation,
            preview_png=preview_png, record=record, pre_bytes=pre_bytes,
            before_png=before_png,
        )

    # -- add-text (pure insertion; no source removal, no redaction) ---------------
    def prepare_insert(
        self, page_index: int, box: Rect, edit: ReplacementEdit,
        preview_zoom: float = 2.0,
    ) -> PreparedEdit:
        """Validate adding new text into an empty box (AGENTS.md §3 'Add new text').

        Same candidate pipeline as replacements, minus redaction: overlap with
        existing text is refused rather than painted over.
        """
        def fail(validation: ValidationResult, issues: list[str]) -> PreparedEdit:
            validation.ok = False
            validation.issues.extend(issues)
            reported = list(dict.fromkeys(validation.issues))
            return PreparedEdit(ok=False, candidate_bytes=None, validation=validation,
                                preview_png=None, record=None, issues=reported)

        if edit.source_revision != self.revision:
            return fail(ValidationResult(ok=False), ["Stale request: the document changed."])

        # refuse to paint over existing text (§9 collateral protection, inverted)
        probe = box.inflated(-2.0, -2.0) if box.width > 10 and box.height > 10 else box
        for ln in extract_page_lines(self.doc[page_index], with_chars=False):
            if ln.bbox.intersects(probe):
                return fail(ValidationResult(ok=False),
                            ["That spot already has text. Choose an empty area of the page."])

        dom = FontReference(
            name=edit.font_override or "Helvetica",
            family=edit.font_override or "Helvetica",
            size=edit.size_override or 11.0,
            bold=bool(edit.bold_override), italic=bool(edit.italic_override),
        )
        size = dom.size
        color: Color = edit.color_override or Color(0.0, 0.0, 0.0)
        resolved = resolve_font(
            self.doc, dom, set(edit.new_text),
            bold=edit.bold_override, italic=edit.italic_override,
            user_choice=edit.font_override,
        )
        validation = ValidationResult(ok=True)
        if resolved.substituted:
            validation.substituted_font = resolved.family
        if resolved.missing_glyphs:
            validation.missing_glyphs = resolved.missing_glyphs
            return fail(validation, [
                f"Font {resolved.family} cannot render: "
                f"{''.join(resolved.missing_glyphs[:10])}"
            ])

        # synthetic region: no source text, geometry = the insertion box
        region = TextRegion(region_id=edit.region_id, lines=[], bbox=box,
                            mode=EditMode.REFLOW_BOX, revision=self.revision,
                            paragraph_box=box)
        pre_bytes = self.doc.tobytes()
        candidate = pymupdf.open(stream=pre_bytes)
        try:
            page = candidate[page_index]
            fontname = self._register_font(page, resolved, f"I{page_index}")

            def _scratch(_page=page, _fontname=fontname,
                         _resolved=resolved) -> pymupdf.Page:
                d = pymupdf.open()
                p = d.new_page(width=_page.rect.width, height=_page.rect.height)
                if _resolved.fontfile:
                    p.insert_font(fontname=_fontname, fontfile=_resolved.fontfile)
                elif _resolved.fontbuffer:
                    p.insert_font(fontname=_fontname, fontbuffer=_resolved.fontbuffer)
                return p

            align = _ALIGN_MAP[edit.alignment]
            spare = page.insert_textbox(
                _to_mupdf_rect(box), edit.new_text, fontsize=size,
                fontname=fontname, align=align, color=color.as_tuple(),
            )
            if spare < 0:
                validation.overflowed = True
                validation.overflow_px = -spare
                validation.required_size = self._fit_size(
                    _scratch, box, edit.new_text, fontname, size, align, color.as_tuple())
                if edit.auto_shrink and validation.required_size >= 4.0:
                    candidate.close()
                    candidate = pymupdf.open(stream=pre_bytes)
                    page = candidate[page_index]
                    fontname = self._register_font(page, resolved, f"I{page_index}R")
                    size = validation.required_size
                    validation.overflowed = False
                    validation.overflow_px = 0.0
                    validation.issues.append(
                        f"Text auto-shrunk to {size:g} pt to fit the box.")
                    spare = page.insert_textbox(
                        _to_mupdf_rect(box), edit.new_text, fontsize=size,
                        fontname=fontname, align=align, color=color.as_tuple())
                    if spare < 0:
                        raise OverflowError("Replacement text overflows the text box.")
                else:
                    raise OverflowError("Replacement text overflows the text box.")

            padded = box.inflated(_PAD, _PAD)
            self._validate_extraction(candidate[page_index], page_index, region, edit,
                                      padded, validation)
            before_png = None
            after = None
            if validation.ok:
                before = self.doc[page_index].get_pixmap(
                    matrix=pymupdf.Matrix(preview_zoom, preview_zoom))
                after = candidate[page_index].get_pixmap(
                    matrix=pymupdf.Matrix(preview_zoom, preview_zoom))
                self._validate_pixels(before, after, self._display_rect(page_index, padded),
                                      preview_zoom, validation)
                before_png = before.tobytes("png")
            if not validation.ok:
                candidate.close()
                return fail(validation, [])

            preview_png = after.tobytes("png")
            cand_bytes = candidate.tobytes(deflate=True)
            candidate.close()
        except OverflowError as exc:
            try:
                candidate.close()
            except Exception:
                pass
            return fail(validation, [str(exc)])
        except Exception as exc:  # engine failure must never corrupt the working doc
            try:
                candidate.close()
            except Exception:
                pass
            return fail(ValidationResult(ok=False), [f"Edit failed: {exc}"])

        record = EditRecord(
            region=None, edit=edit, resolved_family=resolved.family,
            resolved_source=resolved.source, page_index=page_index,
            pre_revision=self.revision, post_revision=self.revision + 1,
        )
        return PreparedEdit(
            ok=True, candidate_bytes=cand_bytes, validation=validation,
            preview_png=preview_png, record=record, pre_bytes=pre_bytes,
            before_png=before_png,
        )

    # -- redaction (M9: "Black Out Text" — true removal, no replacement) ---------
    def prepare_redact(
        self, page_index: int, boxes: list[Rect], preview_zoom: float = 2.0,
    ) -> PreparedEdit:
        """Remove all text under the given engine-space boxes; paint black.

        Redaction is geometry-based (AGENTS.md §9): each marquee is snapped to
        the union of the character boxes it touches (no half-covered glyphs),
        removed on an isolated candidate, and validated before commit. Pages
        with pre-existing redaction annotations are refused (D6): PyMuPDF's
        `apply_redactions` applies every pending annot, so an unrelated annot
        would be swept in. A scanned page (images, no text) is allowed with an
        explicit disclosure: the box covers the image, the scan still has it.
        """
        def fail(validation: ValidationResult, issues: list[str]) -> PreparedEdit:
            validation.ok = False
            validation.issues.extend(issues)
            reported = list(dict.fromkeys(validation.issues))
            return PreparedEdit(ok=False, candidate_bytes=None, validation=validation,
                                preview_png=None, record=None, issues=reported)

        validation = ValidationResult(ok=True)
        if not boxes:
            return fail(validation, ["No black-out areas were marked."])
        page = self.doc[page_index]
        for annot in page.annots() or []:
            if annot.type[0] == pymupdf.PDF_ANNOT_REDACT:
                return fail(validation, [
                    "This page has an existing redaction annotation. Black-out is "
                    "disabled here to avoid applying unrelated redactions."])

        candidate = None
        try:
            pre_bytes = self.doc.tobytes()
            candidate = pymupdf.open(stream=pre_bytes)
            cand_page = candidate[page_index]

            # char-aware snapping with MuPDF's own removal rule: a character is
            # removed iff its box MIDPOINT lies inside the redaction rect
            # (verified empirically — a char whose box merely overlaps survives,
            # even at ~49% coverage). Snapping therefore uses the same midpoint
            # predicate, computed from the ORIGINAL page (pre-mutation geometry)
            # so the validation below compares identical geometry on both sides.
            def _mid(cb: Rect) -> tuple[float, float]:
                return ((cb.x0 + cb.x1) / 2.0, (cb.y0 + cb.y1) / 2.0)

            orig_lines_chars = extract_page_lines(page, with_chars=True)
            all_chars = [(ch, cb) for ln in orig_lines_chars for run in ln.runs
                         if run.char_bboxes and len(run.char_bboxes) == len(run.text)
                         for ch, cb in zip(run.text, run.char_bboxes, strict=True)]
            snapped: list[Rect] = []
            removed_texts: list[str] = []
            scanned_cover = False
            has_images = bool(page.get_images())
            for box in boxes:
                hit_chars: list[Rect] = []
                texts: list[str] = []
                for ch, cb in all_chars:
                    mx, my = _mid(cb)
                    if not (box.x0 <= mx <= box.x1 and box.y0 <= my <= box.y1):
                        continue
                    texts.append(ch)  # spaces keep word boundaries
                    hit_chars.append(cb)
                if not any(not ch.isspace() for ch in texts):
                    if not has_images:
                        return fail(validation, [
                            "One of the marked areas has no text under it — nothing "
                            "to remove there."])
                    # scanned page: nothing removable under the box, but the
                    # user wants it covered — paint-only, with disclosure below
                    snapped.append(box)
                    scanned_cover = True
                    continue
                x0 = min(cb.x0 for cb in hit_chars)
                y0 = min(cb.y0 for cb in hit_chars)
                x1 = max(cb.x1 for cb in hit_chars)
                y1 = max(cb.y1 for cb in hit_chars)
                snapped.append(Rect(x0, y0, x1, y1))
                removed_texts.append(_norm_ws("".join(texts)))

            for rect in snapped:
                cand_page.add_redact_annot(_to_mupdf_rect(rect), fill=(0, 0, 0))
            cand_page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            )

            # Validation is CHAR-level (D18 fix): line bboxes are too coarse —
            # the marked line's own leftovers (e.g. 'End ' + 'ction.' after
            # marking its middle) are legitimate, and neighbouring line boxes
            # can graze the mark's edge in tight leading. Characters are exact:
            #   * every char inside a snapped mark must be gone;
            #   * the multiset of chars outside the marks must be identical
            #     before vs after (same char, same position ±0.1 pt).
            def _chars(lines) -> list[tuple[str, Rect]]:
                out: list[tuple[str, Rect]] = []
                for ln in lines:
                    for run in ln.runs:
                        if run.char_bboxes and len(run.char_bboxes) == len(run.text):
                            out.extend(zip(run.text, run.char_bboxes, strict=True))
                return out

            def _inside(cb: Rect) -> bool:
                mx, my = _mid(cb)
                return any(r.x0 <= mx <= r.x1 and r.y0 <= my <= r.y1 for r in snapped)

            before_chars = _chars(orig_lines_chars)
            after_chars = _chars(extract_page_lines(cand_page, with_chars=True))

            survivors = [(ch, cb) for ch, cb in after_chars if _inside(cb)]
            if survivors:
                sample = "".join(ch for ch, _ in survivors[:12])
                validation.ok = False
                validation.issues.append(
                    f"Verification failed: {len(survivors)} characters inside a "
                    f"black-out area survived removal ({sample!r}…).")
            before_outside = Counter(
                (ch, round(cb.x0, 1), round(cb.y0, 1), round(cb.x1, 1), round(cb.y1, 1))
                for ch, cb in before_chars if not _inside(cb))
            after_outside = Counter(
                (ch, round(cb.x0, 1), round(cb.y0, 1), round(cb.x1, 1), round(cb.y1, 1))
                for ch, cb in after_chars if not _inside(cb))
            lost = before_outside - after_outside
            extra = after_outside - before_outside
            if lost or extra:
                validation.collateral_change = True
            if validation.collateral_change:
                detail = ""
                if lost:
                    ex = "".join(ch for (ch, *_), n in list(lost.items())[:8]
                                 for _ in range(n))
                    detail += f" deleted {sum(lost.values())} chars ({ex!r}…)"
                if extra:
                    detail += f" gained {sum(extra.values())} unexpected chars"
                validation.ok = False
                validation.issues.append(
                    "Edit rejected: text outside the black-out areas would "
                    "change." + detail)
            if not validation.ok:
                candidate.close()
                return fail(validation, [])

            # pixels: only the marked display-space area may change
            change_box = snapped[0]
            for r in snapped[1:]:
                change_box = _union(change_box, r)
            padded = change_box.inflated(_PAD, _PAD)
            before = self.doc[page_index].get_pixmap(
                matrix=pymupdf.Matrix(preview_zoom, preview_zoom))
            after = candidate[page_index].get_pixmap(
                matrix=pymupdf.Matrix(preview_zoom, preview_zoom))
            self._validate_pixels(before, after, self._display_rect(page_index, padded),
                                  preview_zoom, validation)
            if not validation.ok:
                candidate.close()
                return fail(validation, [])

            if scanned_cover:
                validation.issues.append(
                    "Marked area looks scanned (image, no removable text) — the "
                    "black box covers it; the scan itself still contains the "
                    "content.")

            preview_png = after.tobytes("png")
            cand_bytes = candidate.tobytes(deflate=True)
            candidate.close()
        except Exception as exc:  # engine failure must never corrupt the working doc
            if candidate is not None:
                try:
                    candidate.close()
                except Exception:
                    pass
            return fail(ValidationResult(ok=False), [f"Redaction failed: {exc}"])

        record = EditRecord(
            region=None,
            edit=ReplacementEdit(
                region_id=f"redact:p{page_index}:r{self.revision}",
                source_revision=self.revision, new_text="",
                mode=EditMode.REFLOW_BOX, page_index=page_index,
            ),
            resolved_family="", resolved_source="",
            page_index=page_index,
            pre_revision=self.revision, post_revision=self.revision + 1,
            redacted_text=" ".join(removed_texts),
        )
        return PreparedEdit(
            ok=True, candidate_bytes=cand_bytes, validation=validation,
            preview_png=preview_png, record=record, pre_bytes=pre_bytes,
            before_png=before.tobytes("png"),
        )

    # -- batch replacement (M10: Replace All) ------------------------------------
    def prepare_page_edits(
        self, page_index: int, pairs: list[BatchPair], preview_zoom: float = 2.0,
    ) -> tuple[PreparedEdit | None, list[PairOutcome], Rect | None]:
        """Replace several lines of one page in a single validated candidate (M10).

        The Replace-All batching unit: all planned line replacements of one page
        go through one redact + insert + validate pass on an isolated candidate,
        exactly like a single Mode A edit — one commit, one revision bump per
        page. Pairs that cannot work (stale, not editable, missing glyphs,
        would not fit, empty result) are skipped with a reason instead of
        failing the whole page.

        Returns (prepared, outcomes, change_rect): `prepared` is None when every
        pair was skipped; `change_rect` is the display-space union of the
        planned changes (identity for preview cropping), None on failure.
        A page-level validation failure returns ok=False with the planned
        outcomes intact — the caller flips them to skipped.
        """
        outcomes: list[PairOutcome] = []
        planned: list[tuple[BatchPair, ResolvedFont, float, Color]] = []
        font_cache: dict[tuple, ResolvedFont] = {}
        substituted: list[str] = []
        validation = ValidationResult(ok=True)
        all_regions = [p.region for p in pairs]

        def _skip(pair: BatchPair, reason: str) -> None:
            outcomes.append(PairOutcome(pair.match_ids, "skipped", reason))

        for pair in pairs:
            region, edit = pair.region, pair.edit
            edit = _canonical_rtl_text(edit)
            pair.edit = edit
            if region.revision != self.revision or edit.source_revision != self.revision:
                _skip(pair, "The document changed — re-run Replace All.")
                continue
            cap = self.capability(page_index, region)
            if not cap.editable:
                _skip(pair, cap.reason)
                continue
            if edit.mode != EditMode.PRESERVE_LINE:
                _skip(pair, "Replace All rebuilds whole lines; this match has no "
                            "line-level replacement.")
                continue
            new_text = _norm_ws(edit.new_text)
            if not new_text:
                _skip(pair, "Replacement would leave the line empty — use Black "
                            "Out Text to delete text.")
                continue
            dom = region.dominant_font
            size = edit.size_override or dom.size
            color = edit.color_override or dom.color
            cache_key = (dom.name, dom.size, dom.bold, dom.italic,
                         edit.font_override, frozenset(new_text))
            resolved = font_cache.get(cache_key)
            if resolved is None:
                resolved = resolve_font(
                    self.doc, dom, set(new_text),
                    bold=edit.bold_override, italic=edit.italic_override,
                    user_choice=edit.font_override)
                font_cache[cache_key] = resolved
            if resolved.missing_glyphs:
                _skip(pair, f"Font {resolved.family} cannot render: "
                            f"{''.join(resolved.missing_glyphs[:10])}")
                continue

            # Width: LTR pairs measure against the ORIGINAL page (pre-redaction,
            # so a replaced neighbour cell caps at its insertion origin). RTL
            # pairs use the real shaped layout width (text_length is meaningless
            # for bidi output) against the same right-edge limit (D20).
            origin = region.lines[0].runs[0].origin
            used_size = size
            note = ""
            if needs_bidi(new_text):
                limit = self._batch_right_limit(self.doc[page_index], region,
                                                all_regions)
                available = max(8.0, limit - region.lines[0].bbox.x0)
                width = self._shaped_width(resolved, new_text, size, color)
                if width > available:
                    required = self._fit_shaped_size(resolved, new_text, size,
                                                     color, available)
                    if edit.auto_shrink and required:
                        used_size = required
                    else:
                        _skip(pair, f"Replacement is {width - available:.0f} pt wider "
                                    f"than the line's free space — shorten it or turn "
                                    f"on 'Shrink to fit'.")
                        continue
            else:
                limit = self._batch_right_limit(self.doc[page_index], region,
                                                all_regions)
                available = max(8.0, limit - origin.x)
                width = self._measure(resolved, new_text, size)
                if width > available:
                    required = max(4.0, round(size * available / width * 2) / 2)
                    if edit.auto_shrink and required >= 4.0:
                        shrunk = self._measure(resolved, new_text, required)
                        if shrunk > available:
                            _skip(pair, f"Does not fit even shrunk to {required:g} pt — "
                                        f"shorten the replacement.")
                            continue
                        used_size = required
                        width = shrunk
                    else:
                        _skip(pair, f"Replacement is {width - available:.0f} pt wider "
                                    f"than the line's free space — shorten it or turn "
                                    f"on 'Shrink to fit'.")
                        continue
            if used_size != size:
                note = f"Text shrunk to {used_size:g} pt to fit the line."
            if resolved.substituted:
                if resolved.family not in substituted:
                    substituted.append(resolved.family)
                note = (note + " " if note else "") + \
                    f"Original font unavailable. Using {resolved.family}."
            outcomes.append(PairOutcome(
                pair.match_ids, "planned", note, used_size=used_size,
                font_family=resolved.family,
                font_substituted=bool(resolved.substituted)))
            planned.append((pair, resolved, used_size, color))

        if not planned:
            return None, outcomes, None

        if substituted:
            validation.substituted_font = substituted[0]

        pre_bytes = self.doc.tobytes()
        candidate = pymupdf.open(stream=pre_bytes)
        display: Rect | None = None
        try:
            page = candidate[page_index]
            for pair, _resolved, _size, _color in planned:
                self._add_region_redact_annots(page, pair.region)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            )

            font_tags: dict[tuple, str] = {}
            change_box: Rect | None = None
            for n, (pair, resolved, used_size, color) in enumerate(planned):
                line = pair.region.lines[0]
                if needs_bidi(pair.edit.new_text):
                    extent = self._insert_shaped_line(
                        page, pair.region, pair.edit, resolved, used_size, color,
                        self._batch_right_limit(
                            page, pair.region, [p.region for p, *_ in planned]))
                else:
                    key = (resolved.source, resolved.builtin_name, resolved.family)
                    fontname = font_tags.get(key)
                    if fontname is None:
                        fontname = self._register_font(page, resolved, f"B{page_index}N{n}")
                        font_tags[key] = fontname
                    text = _norm_ws(pair.edit.new_text)
                    origin = line.runs[0].origin
                    page.insert_text((origin.x, origin.y), text, fontsize=used_size,
                                     fontname=fontname, color=color.as_tuple())
                    extent = Rect(origin.x, line.bbox.y0,
                                  origin.x + self._measure(resolved, text, used_size),
                                  line.bbox.y1)
                change_box = _union(_union(change_box or pair.region.bbox,
                                           pair.region.bbox), extent)

            padded = change_box.inflated(_PAD, _PAD)
            self._validate_batch_extraction(candidate[page_index], page_index,
                                            planned, padded, validation)
            before_png = None
            after = None
            if validation.ok:
                before = self.doc[page_index].get_pixmap(
                    matrix=pymupdf.Matrix(preview_zoom, preview_zoom))
                after = candidate[page_index].get_pixmap(
                    matrix=pymupdf.Matrix(preview_zoom, preview_zoom))
                display = self._display_rect(page_index, padded)
                self._validate_pixels(before, after, display, preview_zoom, validation)
                before_png = before.tobytes("png")
            if not validation.ok:
                candidate.close()
                return PreparedEdit(
                    ok=False, candidate_bytes=None, validation=validation,
                    preview_png=None, record=None,
                    issues=list(dict.fromkeys(validation.issues))), outcomes, None

            preview_png = after.tobytes("png")
            cand_bytes = candidate.tobytes(deflate=True)
            candidate.close()
        except Exception as exc:  # engine failure must never corrupt the working doc
            try:
                candidate.close()
            except Exception:
                pass
            return PreparedEdit(
                ok=False, candidate_bytes=None,
                validation=ValidationResult(ok=False), preview_png=None, record=None,
                issues=[f"Edit failed: {exc}"]), outcomes, None

        first_pair, first_resolved, _size, _color = planned[0]
        record = EditRecord(
            region=first_pair.region, edit=first_pair.edit,
            resolved_family=first_resolved.family,
            resolved_source=first_resolved.source,
            page_index=page_index,
            pre_revision=self.revision, post_revision=self.revision + 1,
            pairs=[(p.region, p.edit) for p, *_ in planned],
        )
        return PreparedEdit(
            ok=True, candidate_bytes=cand_bytes, validation=validation,
            preview_png=preview_png, record=record, pre_bytes=pre_bytes,
            before_png=before_png,
        ), outcomes, display

    # -- commit ------------------------------------------------------------------
    def commit(self, prepared: PreparedEdit) -> int:
        """Swap the working document for the validated candidate. Returns new revision."""
        if not prepared.ok or prepared.candidate_bytes is None or prepared.record is None:
            raise ValueError("cannot commit a failed edit")
        old = self.doc
        self.doc = pymupdf.open(stream=prepared.candidate_bytes)
        try:
            old.close()
        except Exception:
            pass
        self.revision = prepared.record.post_revision
        return self.revision

    # -- validation ---------------------------------------------------------------
    def _display_rect(self, page_index: int, rect: Rect) -> Rect:
        """Engine space -> display space: pixmaps render the rotated page, so a
        rect used to mask pixel diffs must go through `rotation_matrix`
        (identity at rotation 0)."""
        r = _to_mupdf_rect(rect) * self.doc[page_index].rotation_matrix
        return Rect(r.x0, r.y0, r.x1, r.y1)

    def _baseline_clusters(self, cand_page: pymupdf.Page, padded: Rect) -> list[str]:
        """Norm-cmp'd text per baseline cluster of lines inside `padded` (D20).

        A shaped rebuild can emit its Latin and Arabic parts as separate
        extraction fragments sharing one baseline (sometimes with overlapping
        x-ranges). Joining each baseline cluster x-sorted reconstructs the
        line text; containment checks then work per cluster instead of
        depending on fragment adjacency in the global extraction order.
        """
        inside = [ln for ln in extract_page_lines(cand_page, with_chars=False)
                  if ln.bbox.intersects(padded)]
        inside.sort(key=lambda ln: ln.bbox.y0)
        clusters: list[list] = []
        for ln in inside:
            if clusters and abs(ln.bbox.y0 - clusters[-1][0].bbox.y0) <= 3.0:
                clusters[-1].append(ln)
            else:
                clusters.append([ln])
        return [norm_cmp(" ".join(ln.text for ln
                                  in sorted(group, key=lambda ln: ln.bbox.x0)))
                for group in clusters]

    def _validate_extraction(
        self, cand_page: pymupdf.Page, page_index: int, region: TextRegion,
        edit: ReplacementEdit, padded: Rect, validation: ValidationResult,
    ) -> None:
        cluster_texts = self._baseline_clusters(cand_page, padded)
        inside_text = " ".join(cluster_texts)

        want = norm_cmp(edit.new_text)
        # containment in one baseline cluster (fragmented shaped rebuilds) or
        # in the joined area text (Mode B reflow wraps across baselines)
        if want and (want not in inside_text
                     and not any(want in ct for ct in cluster_texts)):
            validation.ok = False
            validation.issues.append(
                "Verification failed: replacement text not found where expected. "
                f"Found there instead: {inside_text[:80]!r}"
            )
        source_text = norm_cmp(region.text)
        # Retaining the original sentence inside the requested replacement is
        # legitimate (for example, appending a word). Only flag source text
        # that appears in the output but was not requested by the user.
        if source_text and source_text not in want and source_text in inside_text:
            validation.ok = False
            validation.issues.append(
                "Verification failed: original text still present in the edited region."
            )

        self._validate_outside_lines(cand_page, page_index, padded, validation)

    def _validate_outside_lines(
        self, cand_page: pymupdf.Page, page_index: int, padded: Rect,
        validation: ValidationResult,
    ) -> None:
        """Text outside the padded change area must be unchanged (M11 rewrite).

        MuPDF rewrites the page content stream on `apply_redactions`, which
        can REGROUP extraction lines page-wide (table columns merge into row
        lines; measured 137 -> 135 lines on a real file) — a positional
        line-by-line comparison false-alarms on such files. The robust
        invariant is the character INVENTORY of all outside text (order-free,
        NFKC space); pixel validation covers the visual side (moved/swapped
        text cannot hide from it).
        """
        cand_lines = extract_page_lines(cand_page, with_chars=False)
        outside_after = [ln for ln in cand_lines if not ln.bbox.intersects(padded)]
        orig_lines = extract_page_lines(self.doc[page_index], with_chars=False)
        outside_before = [ln for ln in orig_lines if not ln.bbox.intersects(padded)]
        before_text = norm_cmp(
            " ".join(ln.text for ln in outside_before)).replace(" ", "")
        after_text = norm_cmp(
            " ".join(ln.text for ln in outside_after)).replace(" ", "")
        if before_text != after_text:
            validation.collateral_change = True
        if validation.collateral_change:
            lost = Counter(before_text) - Counter(after_text)
            extra = Counter(after_text) - Counter(before_text)
            detail = ""
            if lost:
                sample = "".join(ch for ch, n in list(lost.items())[:12] for _ in range(n))
                detail += f" deleted {sum(lost.values())} chars ({sample!r}…)"
            if extra:
                sample = "".join(ch for ch, n in list(extra.items())[:12] for _ in range(n))
                detail += f" gained {sum(extra.values())} chars ({sample!r}…)"
            validation.ok = False
            validation.issues.append(
                "Edit rejected: text outside the selected region would change."
                + detail
            )

    def _validate_batch_extraction(
        self, cand_page: pymupdf.Page, page_index: int,
        planned: list[tuple[BatchPair, ResolvedFont, float, Color]], padded: Rect,
        validation: ValidationResult,
    ) -> None:
        """Multi-pair line-level check for Replace All (M10, NFKC per D20).

        Line-text multiset inside the padded change area: after must equal
        (before − sources) + wants. Unlike substring containment this survives
        duplicate lines elsewhere in the area (e.g. a match count truncated at
        the cap leaving an identical line untouched next to a replaced one).
        Unchanged neighbours must not move textually; pixels cover the rest.

        Bidi re-fragmentation allowance (D20): a shaped rebuild can split one
        extracted line into per-bidi-run fragments (pure Arabic lines keep one
        line, mixed Arabic/Latin lines split). When the raw multiset differs,
        the page still passes if the joined text is character-identical AND
        every pair's substitution verifiably happened.
        """
        before = Counter(
            norm_cmp(ln.text)
            for ln in extract_page_lines(self.doc[page_index], with_chars=False)
            if ln.bbox.intersects(padded)
        )
        after = Counter(
            norm_cmp(ln.text)
            for ln in extract_page_lines(cand_page, with_chars=False)
            if ln.bbox.intersects(padded)
        )
        sources = Counter(norm_cmp(p.region.text) for p, *_ in planned)
        wants = Counter(t for t in (norm_cmp(p.edit.new_text) for p, *_ in planned) if t)
        expected = (before - sources) + wants
        if after != expected:
            joined_before = norm_cmp(" ".join(before.elements()))
            joined_after = norm_cmp(" ".join(after.elements()))
            # Bidi re-fragmentation + word-order canonicalisation (D20): a
            # shaped rebuild splits lines and yields logical-order Arabic while
            # the source was visual-order — compare the CHARACTER inventory
            # (order-free) and prove the substitution per pair instead.
            before_chars = Counter(joined_before.replace(" ", ""))
            after_chars = Counter(joined_after.replace(" ", ""))
            src_chars = Counter(norm_cmp(
                " ".join(p.region.text for p, *_ in planned)).replace(" ", ""))
            want_chars = Counter(norm_cmp(
                " ".join(p.edit.new_text for p, *_ in planned)).replace(" ", ""))
            reflowed = after_chars == (before_chars - src_chars) + want_chars
            if reflowed:
                # the substitution itself must still be proven per pair:
                # inside one baseline cluster (fragmented shaped rebuilds) or
                # in the joined band text (Mode B reflow wraps baselines)
                cluster_texts = self._baseline_clusters(cand_page, padded)
                joined_after = norm_cmp(" ".join(after.elements()))
                for pair, *_ in planned:
                    w = norm_cmp(pair.edit.new_text)
                    s = norm_cmp(pair.region.text)
                    found = (w in joined_after
                             or any(w in ct for ct in cluster_texts))
                    retained = (s in joined_after
                                and s not in w
                                and not any(s in ct for ct in cluster_texts))
                    if w and not found:
                        reflowed = False
                        break
                    if s and retained:
                        reflowed = False
                        break
            if not reflowed:
                validation.ok = False
                lost = before - after - sources
                extra = after - before - wants
                detail = ""
                if lost:
                    detail += f" lost {sum(lost.values())} line(s) ({list(lost)[:2]!r})"
                if extra:
                    detail += f" gained {sum(extra.values())} line(s) ({list(extra)[:2]!r})"
                validation.issues.append(
                    "Edit rejected: text inside the edited lines would change "
                    "unexpectedly." + detail
                )
        self._validate_outside_lines(cand_page, page_index, padded, validation)

    def _validate_pixels(
        self, before: pymupdf.Pixmap, after: pymupdf.Pixmap,
        changed: Rect, zoom: float, validation: ValidationResult,
    ) -> None:
        import numpy as np

        if before.width != after.width or before.height != after.height:
            validation.ok = False
            validation.issues.append("Pixel validation failed: page size changed.")
            return
        a = np.frombuffer(before.samples, dtype=np.uint8).reshape(
            before.height, before.width, before.n)[:, :, :3].astype(np.int16)
        b = np.frombuffer(after.samples, dtype=np.uint8).reshape(
            after.height, after.width, after.n)[:, :, :3].astype(np.int16)
        mask = np.ones(a.shape[:2], dtype=bool)
        x0 = max(0, int(changed.x0 * zoom) - 2)
        y0 = max(0, int(changed.y0 * zoom) - 2)
        x1 = min(a.shape[1], int(changed.x1 * zoom) + 2)
        y1 = min(a.shape[0], int(changed.y1 * zoom) + 2)
        mask[y0:y1, x0:x1] = False
        diff = np.abs(a - b).max(axis=2)
        bad = int((diff[mask] > 24).sum())
        outside = int(mask.sum())
        if outside > 0 and bad / outside > 0.0005:
            validation.ok = False
            validation.issues.append(
                f"Edit rejected: unexpected visual change outside the region "
                f"({bad} pixels differ)."
            )


class _RetryWithSize(Exception):
    """Internal: restart candidate build with an auto-shrunk font size."""

    def __init__(self, size: float):
        super().__init__(size)
        self.size = size
