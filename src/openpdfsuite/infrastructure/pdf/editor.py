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

from collections.abc import Callable
from dataclasses import dataclass, field

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

_ALIGN_MAP = {
    TextAlignment.LEFT: pymupdf.TEXT_ALIGN_LEFT,
    TextAlignment.CENTER: pymupdf.TEXT_ALIGN_CENTER,
    TextAlignment.RIGHT: pymupdf.TEXT_ALIGN_RIGHT,
    TextAlignment.JUSTIFY: pymupdf.TEXT_ALIGN_JUSTIFY,
}

_PAD = 1.5  # pt inflation around the changed box for inside/outside splitting


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


def _to_mupdf_rect(r: Rect) -> pymupdf.Rect:
    return pymupdf.Rect(r.x0, r.y0, r.x1, r.y1)


def _union(a: Rect, b: Rect) -> Rect:
    return Rect(min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1))


def region_page_index(region: TextRegion) -> int:
    """Page index encoded in a region id (`<doc>:r<rev>:p<page>:k...`)."""
    part = region.region_id.split(":p", 1)[1]
    return int(part.split(":", 1)[0])


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

    def _redact_region(self, page: pymupdf.Page, region: TextRegion) -> None:
        """Char-aware bounded redaction of exactly the source text (D6).

        Consecutive char boxes are merged into segments to keep the annot count
        sane while staying tight enough that neighbors are not touched.
        `fill=False` means no paint-over: backgrounds survive.
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
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )

    def _insert_mode_a(
        self, page: pymupdf.Page, region: TextRegion, edit: ReplacementEdit,
        resolved: ResolvedFont, fontname: str, size: float, color: Color,
        validation: ValidationResult,
    ) -> tuple[float, Rect | None]:
        """Insert one line at the original baseline. Returns (used_size, extent)."""
        origin = region.lines[0].runs[0].origin
        text = _norm_ws(edit.new_text)
        width = self._measure(resolved, text, size)
        limit = self._right_neighbor_limit(page, region)
        available = max(8.0, limit - origin.x)
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
                        used_size, extent = self._insert_mode_a(
                            page, region, edit, resolved, fontname, size, color, validation)
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

    def _validate_extraction(
        self, cand_page: pymupdf.Page, page_index: int, region: TextRegion,
        edit: ReplacementEdit, padded: Rect, validation: ValidationResult,
    ) -> None:
        cand_lines = extract_page_lines(cand_page, with_chars=False)
        inside = [ln for ln in cand_lines if ln.bbox.intersects(padded)]
        outside_after = [ln for ln in cand_lines if not ln.bbox.intersects(padded)]
        inside_text = _norm_ws(" ".join(ln.text for ln in inside))

        want = _norm_ws(edit.new_text)
        if want and _norm_ws(inside_text) != want and want not in inside_text:
            validation.ok = False
            validation.issues.append(
                "Verification failed: replacement text not found where expected."
            )
        source_text = _norm_ws(region.text)
        # Retaining the original sentence inside the requested replacement is
        # legitimate (for example, appending a word). Only flag source text
        # that appears in the output but was not requested by the user.
        if source_text and source_text not in want and source_text in inside_text:
            validation.ok = False
            validation.issues.append(
                "Verification failed: original text still present in the edited region."
            )

        orig_lines = extract_page_lines(self.doc[page_index], with_chars=False)
        outside_before = [ln for ln in orig_lines if not ln.bbox.intersects(padded)]
        if len(outside_before) != len(outside_after):
            validation.collateral_change = True
        else:
            for b, a in zip(outside_before, outside_after, strict=True):
                if _norm_ws(b.text) != _norm_ws(a.text) or (
                    abs(b.bbox.x0 - a.bbox.x0) > 1.5 or abs(b.bbox.y0 - a.bbox.y0) > 1.5
                ):
                    validation.collateral_change = True
                    break
        if validation.collateral_change:
            validation.ok = False
            validation.issues.append(
                "Edit rejected: text outside the selected region would change."
            )

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
