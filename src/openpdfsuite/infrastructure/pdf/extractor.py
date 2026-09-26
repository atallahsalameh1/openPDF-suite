"""Structured text extraction: PyMuPDF rawdict -> domain models.

All geometry produced here is in PyMuPDF's page coordinate space (the same
space `search_for`, `add_redact_annot` and pixmaps use, i.e. rotation-aware
display space). Consumers needing unrotated user space go through
`domain.coordinates`.

Region ids are revision-scoped: `f"{doc_id}:r{rev}:p{page}:k{kind}:{idx}"`.
"""

from __future__ import annotations

import pymupdf

from ...domain.models import (
    Color,
    Direction,
    EditMode,
    FontReference,
    PageGeometry,
    Point,
    Rect,
    TextLine,
    TextRegion,
    TextRun,
)

_SPAN_FLAG_SUPERSCRIPT = 1
_SPAN_FLAG_ITALIC = 2
_SPAN_FLAG_SERIF = 4
_SPAN_FLAG_MONO = 8
_SPAN_FLAG_BOLD = 16

# Paragraph grouping tolerances (AGENTS.md §8). Kept as named constants so the
# rule reads as prose at the call site.
_MIN_MERGE_OVERLAP = 0.6  # x-overlap of a pair, as a fraction of the narrower line
_GAP_MAX_FACTOR = 0.9  # gap must be <= this * median line height
_GAP_MIN_FACTOR = -0.2  # lines may overlap vertically by up to 20% of median
_BLOCKING_OVERLAP = 0.6  # third line must overlap the pair this much to block
# Above this line count the O(n^2) blocker scan is skipped. Real pages are far
# below it; a pathological page falls back to pair-local geometry rather than
# stalling extraction.
_MAX_LINES_FOR_BLOCKER_SCAN = 2000


def _rect(t) -> Rect:
    return Rect(t[0], t[1], t[2], t[3])


def _clean_family(name: str) -> str:
    """Strip subset prefix ('ABCDEF+Arial' -> 'Arial') and normalize suffixes."""
    if len(name) > 7 and name[6] == "+":
        name = name[7:]
    return name


def page_geometry(page: pymupdf.Page) -> PageGeometry:
    mb = page.mediabox
    cb = page.cropbox
    rot = page.rotation % 360
    rect = page.rect  # rotated, cropped display rect
    return PageGeometry(
        index=page.number,
        mediabox=_rect(mb),
        cropbox=_rect(cb),
        rotation=rot,
        width=rect.width,
        height=rect.height,
    )


def extract_page_lines(page: pymupdf.Page, with_chars: bool = True) -> list[TextLine]:
    """Extract all horizontal text lines of a page in display space."""
    data = page.get_text("rawdict" if with_chars else "dict")
    lines: list[TextLine] = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for ln in block.get("lines", []):
            wmode = ln.get("wmode", 0)
            direction = Direction.HORIZONTAL if wmode == 0 else Direction.VERTICAL
            runs: list[TextRun] = []
            for span in ln.get("spans", []):
                text = "".join(ch["c"] for ch in span.get("chars", [])) if with_chars else span.get("text", "")
                if not text:
                    continue
                flags = span.get("flags", 0)
                raw_name = span.get("font", "")
                font = FontReference(
                    name=raw_name,
                    family=_clean_family(raw_name),
                    size=float(span.get("size", 12.0)),
                    bold=bool(flags & _SPAN_FLAG_BOLD),
                    italic=bool(flags & _SPAN_FLAG_ITALIC),
                    monospace=bool(flags & _SPAN_FLAG_MONO),
                    serif=bool(flags & _SPAN_FLAG_SERIF),
                    color=Color.from_int(span.get("color", 0)),
                )
                chars = span.get("chars", [])
                char_bboxes = [_rect(c["bbox"]) for c in chars] if with_chars and chars else None
                origin = span.get("origin")
                bbox = _rect(span["bbox"]) if "bbox" in span else (
                    Rect(
                        min(c.x0 for c in char_bboxes), min(c.y0 for c in char_bboxes),
                        max(c.x1 for c in char_bboxes), max(c.y1 for c in char_bboxes),
                    ) if char_bboxes else Rect(0, 0, 0, 0)
                )
                runs.append(TextRun(
                    text=text,
                    font=font,
                    bbox=bbox,
                    origin=Point(origin[0], origin[1]) if origin else Point(bbox.x0, bbox.y1),
                    direction=direction,
                    char_bboxes=char_bboxes,
                ))
            if not runs:
                continue
            x0 = min(r.bbox.x0 for r in runs)
            y0 = min(r.bbox.y0 for r in runs)
            x1 = max(r.bbox.x1 for r in runs)
            y1 = max(r.bbox.y1 for r in runs)
            baseline_y = runs[0].origin.y
            lines.append(TextLine(runs=runs, bbox=Rect(x0, y0, x1, y1),
                                  baseline_y=baseline_y, direction=direction))
    lines.sort(key=lambda ln: (round(ln.bbox.y0, 1), ln.bbox.x0))
    return lines


def _line_dominant_font(line: TextLine) -> FontReference:
    best = max(line.runs, key=lambda r: len(r.text))
    return best.font


def _fonts_compatible(a: FontReference, b: FontReference) -> bool:
    return (
        a.family.lower() == b.family.lower()
        and abs(a.size - b.size) <= 0.6
        and a.bold == b.bold
        and a.italic == b.italic
    )


def group_paragraphs(lines: list[TextLine]) -> list[list[TextLine]]:
    """Conservative paragraph grouping (AGENTS.md §8).

    Merge consecutive lines only when ALL hold:
      * both horizontal
      * vertical gap <= 0.9 * median line height (tight leading)
      * horizontal span overlap >= 60% (same column)
      * same dominant font family/size/weight (no style break)
      * no third line sits in the gap between them (see below)

    Anything ambiguous stays line-level; we never merge across columns or
    table-like gaps.

    The last condition is what distinguishes a table from a paragraph. Table
    rows are evenly spaced, share x-ranges and often share fonts, so every
    pair-local test passes and a whole column of rows would collapse into one
    region. The difference is outside the pair: at a table row boundary the
    cell's neighbours have text at the same baseline, while a real paragraph
    line has empty space beside it. A third line inside the pair's gap, within
    the pair's x-overlap zone, means table — not paragraph.
    """
    horizontal = [ln for ln in lines if ln.direction == Direction.HORIZONTAL]
    if not horizontal:
        return [[ln] for ln in lines]
    heights = sorted(ln.bbox.height for ln in horizontal)
    median_h = heights[len(heights) // 2]
    if median_h <= 0:
        # degenerate page (no measurable line height): merging has no sane
        # gap window, so keep everything line-level rather than guess.
        return [[ln] for ln in lines]
    scan_blockers = len(horizontal) <= _MAX_LINES_FOR_BLOCKER_SCAN

    groups: list[list[TextLine]] = []
    current: list[TextLine] = []
    for line in horizontal:
        if not current:
            current = [line]
            continue
        prev = current[-1]
        gap = line.bbox.y0 - prev.bbox.y1
        overlap_x0 = max(prev.bbox.x0, line.bbox.x0)
        overlap_x1 = min(prev.bbox.x1, line.bbox.x1)
        overlap = max(0.0, overlap_x1 - overlap_x0)
        min_width = min(prev.bbox.width, line.bbox.width)
        overlap_ratio = overlap / min_width if min_width > 0 else 0.0
        same_font = _fonts_compatible(_line_dominant_font(prev), _line_dominant_font(line))
        mergeable = (
            _GAP_MIN_FACTOR * median_h <= gap <= _GAP_MAX_FACTOR * median_h
            and overlap_ratio >= _MIN_MERGE_OVERLAP
            and same_font
            and not (scan_blockers and _blocked_by_other_line(
                horizontal, prev, line, overlap_x0, overlap_x1))
        )
        if mergeable:
            current.append(line)
        else:
            groups.append(current)
            current = [line]
    if current:
        groups.append(current)
    return groups


def _blocked_by_other_line(
    horizontal: list[TextLine],
    prev: TextLine,
    line: TextLine,
    overlap_x0: float,
    overlap_x1: float,
) -> bool:
    """True when a third line sits in the gap, inside the pair's x-overlap.

    That pattern is a table row boundary (neighbouring cells on the same
    baseline), never a paragraph line break.
    """
    if overlap_x1 <= overlap_x0:
        return False
    for other in horizontal:
        if other is prev or other is line:
            continue
        # vertically inside the open gap (touching edges do not count)
        if other.bbox.y1 <= prev.bbox.y1 or other.bbox.y0 >= line.bbox.y0:
            continue
        # and horizontally inside the pair's shared x-range
        ix0 = max(overlap_x0, other.bbox.x0)
        ix1 = min(overlap_x1, other.bbox.x1)
        ix = ix1 - ix0
        if ix <= 0:
            continue
        narrower = min(overlap_x1 - overlap_x0, other.bbox.width)
        if narrower > 0 and ix / narrower >= _BLOCKING_OVERLAP:
            return True
    return False


def build_regions(
    page: pymupdf.Page,
    doc_id: str,
    revision: int,
    include_paragraphs: bool = True,
) -> list[TextRegion]:
    """Build revision-scoped editable regions for one page.

    Every horizontal line becomes a PRESERVE_LINE region. Line groups of 2+
    lines additionally become a REFLOW_BOX region (paragraph mode), with the
    paragraph box = union bbox (inflated slightly on the right/bottom to give
    reflow room without touching neighbors).

    Lines that grouping declined because another line sits beside them (a
    table-like layout, see `group_paragraphs`) carry a `grouping_reason` so the
    UI can explain why the region is line-level rather than silent about it
    (AGENTS.md §8: "offer line-level editing or reject the region with an
    explanation").
    """
    lines = extract_page_lines(page)
    regions: list[TextRegion] = []
    page_index = page.number

    groups = group_paragraphs(lines) if include_paragraphs else []
    in_para = {id(ln) for g in groups if len(g) >= 2 for ln in g}
    table_like = _lines_in_table_context(lines) if include_paragraphs else set()

    for i, line in enumerate(lines):
        if line.direction != Direction.HORIZONTAL:
            continue
        reason = ""
        if include_paragraphs and id(line) in table_like and id(line) not in in_para:
            reason = ("Adjacent text on the same baseline — treated as a table row, "
                      "so this edits as a single line.")
        regions.append(TextRegion(
            region_id=f"{doc_id}:r{revision}:p{page_index}:k line:{i}",
            lines=[line],
            bbox=line.bbox,
            mode=EditMode.PRESERVE_LINE,
            revision=revision,
            grouping_reason=reason,
        ))

    if include_paragraphs:
        for g in groups:
            if len(g) < 2:
                continue
            x0 = min(ln.bbox.x0 for ln in g)
            y0 = min(ln.bbox.y0 for ln in g)
            x1 = max(ln.bbox.x1 for ln in g)
            y1 = max(ln.bbox.y1 for ln in g)
            bbox = Rect(x0, y0, x1, y1)
            regions.append(TextRegion(
                region_id=f"{doc_id}:r{revision}:p{page_index}:k para:{id(g) % 10**6}",
                lines=g,
                bbox=bbox,
                mode=EditMode.REFLOW_BOX,
                revision=revision,
                paragraph_box=bbox.inflated(0, 1.0),
            ))
    return regions


def _lines_in_table_context(lines: list[TextLine]) -> set[int]:
    """Ids of horizontal lines that have a same-baseline neighbour elsewhere.

    In a table, a row's cells share a baseline: several lines with (nearly)
    equal y0 and disjoint x-ranges. In running text a line is alone on its
    baseline. Used only to explain a line-level region in the UI, never to
    change geometry.
    """
    horizontal = [ln for ln in lines if ln.direction == Direction.HORIZONTAL]
    if len(horizontal) < 2:
        return set()
    heights = sorted(ln.bbox.height for ln in horizontal)
    median_h = heights[len(heights) // 2]
    tol = max(1.0, 0.25 * median_h) if median_h > 0 else 1.0
    rows: list[list[TextLine]] = []
    for line in sorted(horizontal, key=lambda ln: ln.bbox.y0):
        if rows and abs(line.bbox.y0 - rows[-1][0].bbox.y0) <= tol:
            rows[-1].append(line)
        else:
            rows.append([line])
    result: set[int] = set()
    for row in rows:
        if len(row) < 2:
            continue
        # cells of one row must be genuinely side by side, not overlapping
        spans = sorted((ln.bbox.x0, ln.bbox.x1) for ln in row)
        side_by_side = all(spans[i + 1][0] >= spans[i][1] - 1.0
                           for i in range(len(spans) - 1))
        if side_by_side:
            result.update(id(ln) for ln in row)
    return result


def page_text_lines_text(page: pymupdf.Page) -> list[str]:
    """Plain line texts, used by validation to detect collateral changes."""
    return [ln.text for ln in extract_page_lines(page, with_chars=False)]


def find_line_containing(page: pymupdf.Page, needle: str) -> TextLine | None:
    for line in extract_page_lines(page):
        if needle in line.text:
            return line
    return None
