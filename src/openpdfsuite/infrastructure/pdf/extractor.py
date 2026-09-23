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
    Anything ambiguous stays line-level; we never merge across columns or
    table-like gaps.
    """
    horizontal = [ln for ln in lines if ln.direction == Direction.HORIZONTAL]
    if not horizontal:
        return [[ln] for ln in lines]
    heights = sorted(ln.bbox.height for ln in horizontal)
    median_h = heights[len(heights) // 2]

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
            -0.2 * median_h <= gap <= 0.9 * median_h
            and overlap_ratio >= 0.6
            and same_font
        )
        if mergeable:
            current.append(line)
        else:
            groups.append(current)
            current = [line]
    if current:
        groups.append(current)
    return groups


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
    """
    lines = extract_page_lines(page)
    regions: list[TextRegion] = []
    page_index = page.number

    for i, line in enumerate(lines):
        if line.direction != Direction.HORIZONTAL:
            continue
        regions.append(TextRegion(
            region_id=f"{doc_id}:r{revision}:p{page_index}:k line:{i}",
            lines=[line],
            bbox=line.bbox,
            mode=EditMode.PRESERVE_LINE,
            revision=revision,
        ))

    if include_paragraphs:
        for g in group_paragraphs(lines):
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


def page_text_lines_text(page: pymupdf.Page) -> list[str]:
    """Plain line texts, used by validation to detect collateral changes."""
    return [ln.text for ln in extract_page_lines(page, with_chars=False)]


def find_line_containing(page: pymupdf.Page, needle: str) -> TextLine | None:
    for line in extract_page_lines(page):
        if needle in line.text:
            return line
    return None
