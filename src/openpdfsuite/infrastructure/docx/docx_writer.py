"""PDF -> DOCX conversion.

This is the worker-side writer. It receives an open PyMuPDF document, walks
each page, and emits a real `.docx` file. Fidelity model (v1, see
docs/editing-limitations.md "Conversion fidelity"):

* Paragraphs are reconstructed by merging tightly-leaded lines (the
  extractor's `group_paragraphs`), with alignment (left / center / right /
  justify) and leading inferred from geometry against the page's content
  margins. This keeps Word's own paragraph spacing from inflating the text.
* Tables drawn with vector rulings are detected via PyMuPDF `find_tables`
  and become real Word tables; pipe-delimited text rows remain a fallback.
* Images are embedded inline in reading order at their on-page size.
* One Word section per PDF page (exact size/orientation, margins fitted to
  the page content) — sections paginate the document, so no manual page
  breaks are emitted and no blank pages appear between PDF pages.

Public API: `write_docx(pymupdf_doc, options, dest) -> DocxExportResult`.

Coordinate convention: extracted geometry is in PyMuPDF's display-space
(see extractor.py docstring). DOCX page/image sizes are EMU-based; we pass
python-docx `Pt()` lengths directly since 1 pt = 1/72" in both spaces.
"""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

import pymupdf
from docx import Document
from docx.enum.section import WD_ORIENTATION, WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from ...domain.models import (
    Color,
    Direction,
    DocxExportOptions,
    DocxExportResult,
    Rect,
    TextLine,
    TextRun,
    UnsupportedItem,
)
from ..pdf.extractor import extract_page_lines, group_paragraphs, page_geometry

# Errors ---------------------------------------------------------------------

class DocxExportError(Exception):
    """User-facing error raised by the DOCX writer (actionable message)."""


# Helpers --------------------------------------------------------------------

def _rect(t) -> Rect:
    return Rect(t[0], t[1], t[2], t[3])


def _hex_color(c: Color) -> str:
    """sRGB color 0..1 -> 6-digit hex string (RRGGBB)."""
    r = max(0, min(255, int(round(c.r * 255))))
    g = max(0, min(255, int(round(c.g * 255))))
    b = max(0, min(255, int(round(c.b * 255))))
    return f"{r:02X}{g:02X}{b:02X}"


def _apply_run_format(run, font) -> None:
    """Apply font family / size / color / bold / italic to a python-docx Run."""
    name = font.family or font.name
    if name:
        # python-docx wants the family; keep it short
        run.font.name = name.split(",")[0].strip().strip('"').strip("'") or name
    if font.size:
        try:
            run.font.size = Pt(max(1.0, float(font.size)))
        except (TypeError, ValueError):
            pass
    if font.bold:
        run.bold = True
    if font.italic:
        run.italic = True
    rgb = _hex_color(font.color)
    if rgb != "000000":  # black is the default; don't write it
        run.font.color.rgb = RGBColor.from_string(rgb)


def _quiet_paragraph(para) -> None:
    """Neutralize the template's paragraph spacing so PDF gaps drive layout."""
    pf = para.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.line_spacing = 1.0


def _emit_runs(para, group: list[TextLine]) -> None:
    """Append a merged paragraph's runs, joining lines with smart separators.

    Line joins insert a space unless the previous line ends with a hyphen and
    the next starts lowercase (dehyphenation). Non-breaking spaces PyMuPDF
    emits for layout reasons become regular spaces.
    """
    last = None
    for li, line in enumerate(group):
        runs = [r for r in line.runs if r.text]
        for ri, run in enumerate(runs):
            text = run.text.replace("\xa0", " ")
            if li > 0 and ri == 0 and last is not None:
                if last.text.endswith("-") and text[:1].islower():
                    last.text = last.text[:-1]
                elif not last.text.endswith(" ") and not text.startswith(" "):
                    text = " " + text
            r = para.add_run(text)
            _apply_run_format(r, run.font)
            last = r


def _union_bbox(bboxes) -> Rect:
    return Rect(
        min(b.x0 for b in bboxes),
        min(b.y0 for b in bboxes),
        max(b.x1 for b in bboxes),
        max(b.y1 for b in bboxes),
    )


def _dominant_size(group: list[TextLine]) -> float:
    best = max(
        (r for ln in group for r in ln.runs),
        key=lambda r: len(r.text), default=None,
    )
    return float(best.font.size) if best is not None and best.font.size else 11.0


def _infer_alignment(group: list[TextLine], left: float, right: float,
                     page_width: float):
    """Guess left/center/right/justify from the group's geometry vs margins.

    Center also accepts symmetry about the page center: a wide centered
    heading may itself define the content right edge, which would otherwise
    make it look right-aligned.
    """
    tol = max(4.0, 0.015 * (right - left))
    x0 = min(ln.bbox.x0 for ln in group)
    x1 = max(ln.bbox.x1 for ln in group)
    if len(group) >= 2:
        body_spans = all(
            abs(ln.bbox.x0 - left) <= tol and abs(ln.bbox.x1 - right) <= tol
            for ln in group[:-1]
        )
        if body_spans:
            return WD_ALIGN_PARAGRAPH.JUSTIFY
    left_in, right_in = x0 - left, right - x1
    center = (x0 + x1) / 2
    if left_in > tol and (
            abs(center - (left + right) / 2) <= tol
            or abs(center - page_width / 2) <= tol):
        return WD_ALIGN_PARAGRAPH.CENTER
    if left_in > tol and right_in <= tol:
        return WD_ALIGN_PARAGRAPH.RIGHT
    return WD_ALIGN_PARAGRAPH.LEFT


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _content_margins(geom, rects) -> tuple[float, float, float, float]:
    """Fit section margins to the page content so text lands where it did."""
    left = min((r.x0 for r in rects), default=72.0)
    top = min((r.y0 for r in rects), default=72.0)
    right_edge = max((r.x1 for r in rects), default=geom.width - 72.0)
    bottom_edge = max((r.y1 for r in rects), default=geom.height - 72.0)
    return (
        _clamp(left, 0.0, geom.width * 0.45),
        _clamp(geom.width - right_edge, 0.0, geom.width * 0.45),
        _clamp(top, 0.0, geom.height * 0.45),
        _clamp(geom.height - bottom_edge, 0.0, geom.height * 0.45),
    )


def _configure_section(section, page, margins) -> None:
    """Page size/orientation/margins for one PDF page's Word section."""
    left, right, top, bottom = margins
    geom = page_geometry(page)
    section.orientation = (WD_ORIENTATION.LANDSCAPE if geom.width > geom.height
                           else WD_ORIENTATION.PORTRAIT)
    # python-docx >= 1.2 does not swap page_width/height when orientation is
    # set, so the display-space width/height are written as-is.
    section.page_width = Pt(geom.width)
    section.page_height = Pt(geom.height)
    section.left_margin = Pt(left)
    section.right_margin = Pt(right)
    section.top_margin = Pt(top)
    section.bottom_margin = Pt(bottom)


# Detectors ------------------------------------------------------------------

class ColumnDetector:
    """Cluster lines into x-bands so multi-column pages can be preserved.

    Strategy: sort each line's left edge (x0); find the largest gap between
    adjacent x0s; if that gap exceeds `min_gap_pt`, split there. Recurse on the
    resulting groups. Returns groups ordered left-to-right, each with its
    lines ordered top-to-bottom.
    """

    def __init__(self, min_gap_pt: float = 30.0, min_lines: int = 4) -> None:
        self.min_gap_pt = min_gap_pt
        self.min_lines = min_lines  # need at least this many lines to bother

    def detect(self, lines: list[TextLine]) -> list[list[TextLine]]:
        horizontals = [ln for ln in lines if ln.direction == Direction.HORIZONTAL]
        if len(horizontals) < self.min_lines:
            return [horizontals]
        bands = self._split(horizontals)
        # one band = not multi-column
        if len(bands) < 2:
            return [horizontals]
        # sort each band top-to-bottom
        for band in bands:
            band.sort(key=lambda ln: (ln.bbox.y0, ln.bbox.x0))
        return bands

    def _split(self, lines: list[TextLine]) -> list[list[TextLine]]:
        if not lines:
            return []
        indexed = sorted(lines, key=lambda ln: ln.bbox.x0)
        xs = [ln.bbox.x0 for ln in indexed]
        # find the largest gap
        best_idx = -1
        best_gap = self.min_gap_pt
        for i in range(len(xs) - 1):
            gap = xs[i + 1] - xs[i]
            if gap > best_gap:
                best_gap = gap
                best_idx = i
        if best_idx < 0:
            return [indexed]
        left = self._split(indexed[: best_idx + 1])
        right = self._split(indexed[best_idx + 1:])
        return left + right


class TableDetector:
    """Detect pipe-delimited tables (`a | b | c` rows).

    Strategy: a row is a text line containing `|`. Consecutive `|`-lines with
    the same number of `|` cells form a table. Returns rows of cells (each cell
    is the list of TextRuns that produced the cell text).
    """

    def __init__(self, min_rows: int = 2, min_cols: int = 2) -> None:
        self.min_rows = min_rows
        self.min_cols = min_cols

    def detect(self, lines: list[TextLine]) -> list[list[list[TextRun]]] | None:
        """Return rows->cells->runs, or None when no table is detected."""
        detailed = self.detect_detailed(lines)
        return None if detailed is None else detailed[0]

    def detect_detailed(
            self, lines: list[TextLine],
    ) -> tuple[list[list[list[TextRun]]], list[TextLine]] | None:
        """Like `detect`, but also returns the consumed TextLines.

        The writer needs the source lines so it can exclude them from the
        flowing-paragraph path instead of dropping every non-table line on
        the page (the v1 behavior).
        """
        horizontals = [ln for ln in lines if ln.direction == Direction.HORIZONTAL]
        pipe_lines = [ln for ln in horizontals if "|" in ln.text]
        if len(pipe_lines) < self.min_rows:
            return None
        # split each line by '|' into cell text fragments; align by count.
        cell_counts = [ln.text.count("|") + 1 for ln in pipe_lines]
        target_cols = max(set(cell_counts), key=cell_counts.count)
        if target_cols < self.min_cols:
            return None
        # keep only lines whose cell count matches the modal count
        kept = [ln for ln, c in zip(pipe_lines, cell_counts, strict=True)
                if c == target_cols]
        if len(kept) < self.min_rows:
            return None
        rows: list[list[list[TextRun]]] = []
        for line in kept:
            rows.append(self._split_line_into_cells(line, target_cols))
        return rows, kept

    def _split_line_into_cells(self, line: TextLine, n_cells: int
                               ) -> list[list[TextRun]]:
        """Split a TextLine into N cells by `|` characters.

        For the common case (one run per line), each cell becomes a single run
        with the cell's text and the line's font. Multi-run lines fall back to
        placing the whole line in the first cell — table detection is rare
        enough that this is acceptable; richer splitting can come later.
        """
        text = line.text
        # positions of the `|` chars
        splits = [i for i, ch in enumerate(text) if ch == "|"]
        if len(splits) != n_cells - 1:
            return [[r for r in line.runs]]
        # boundaries exclude the `|` itself: end of cell ci = position of the
        # (ci)th `|`; start of cell ci+1 = that position + 1.
        boundaries = [0]
        for s in splits:
            boundaries.append(s)
            boundaries.append(s + 1)
        boundaries.append(len(text))
        # boundaries now has 2*n_cells - 1 entries; we use pairs (even, odd).
        if len(line.runs) <= 1:
            template = line.runs[0] if line.runs else None
            cells: list[list[TextRun]] = []
            for ci in range(n_cells):
                lo = boundaries[2 * ci]
                hi = boundaries[2 * ci + 1]
                cell_text = text[lo:hi]
                if template is None or not cell_text:
                    cells.append([])
                else:
                    cells.append([TextRun(
                        text=cell_text,
                        font=template.font,
                        bbox=template.bbox,
                        origin=template.origin,
                        direction=template.direction,
                    )])
            return cells
        # multi-run fallback: whole line in the first cell, blanks elsewhere
        blank = [[]] * (n_cells - 1)
        return [[r for r in line.runs], *blank]


def _find_geometric_tables(page) -> list[tuple[Rect, list[list[Rect | None]]]]:
    """Real ruled tables via PyMuPDF find_tables (needs >= 2x2 cells).

    Returns (table bbox, rows of cell rects) pairs; empty when none. Lone
    filled rectangles and divider lines do not qualify, so page backgrounds
    survive as backgrounds instead of becoming 1x1 tables.
    """
    try:
        finder = page.find_tables()
    except Exception:
        return []
    out: list[tuple[Rect, list[list[Rect | None]]]] = []
    for table in getattr(finder, "tables", []):
        try:
            if table.row_count < 2 or table.col_count < 2:
                continue
            rows: list[list[Rect | None]] = []
            for row in table.rows:
                rows.append([_rect(cell) if cell is not None else None
                             for cell in row.cells])
            out.append((_rect(table.bbox), rows))
        except Exception:
            continue
    return out


def _line_center_in(bbox, rect, pad: float = 3.0) -> bool:
    cx = (bbox.x0 + bbox.x1) / 2
    cy = (bbox.y0 + bbox.y1) / 2
    return (rect.x0 - pad) <= cx <= (rect.x1 + pad) and \
        (rect.y0 - pad) <= cy <= (rect.y1 + pad)


def _page_image_items(page) -> list[tuple[Rect, bytes]]:
    """Placed images on the page as (bbox, image bytes), reading order."""
    items: list[tuple[Rect, bytes]] = []
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return items
    seen: set[tuple[int, float, float]] = set()
    for info in infos:
        bbox_raw = info.get("bbox")
        xref = info.get("xref", 0)
        if not bbox_raw or not xref or xref < 0:
            continue
        bbox = _rect(bbox_raw)
        if bbox.width < 3 or bbox.height < 3:
            continue
        key = (int(xref), round(bbox.x0, 1), round(bbox.y0, 1))
        if key in seen:
            continue
        seen.add(key)
        try:
            extracted = page.parent.extract_image(int(xref))
        except Exception:
            continue
        data = extracted.get("image")
        if data:
            items.append((bbox, data))
    items.sort(key=lambda t: (t[0].y0, t[0].x0))
    return items


class ImageEmbedder:
    """Legacy top-of-page image embedder (kept for direct/simple use).

    The main export path uses `_page_image_items` + `_emit_image`, which place
    images inline in reading order at their on-page size.
    """

    def __init__(self, max_width_pt: float = 360.0) -> None:
        self.max_width_pt = max_width_pt

    def embed_for_page(self, doc: Document, page) -> int:
        """Return the number of images embedded for this page."""
        try:
            images = page.get_images(full=True)
        except Exception:
            return 0
        count = 0
        for entry in images:
            xref = entry[0]
            try:
                info = page.parent.extract_image(xref)
            except Exception:
                continue
            data = info.get("image")
            if not data:
                continue
            width = info.get("width") or 1
            target_w = min(self.max_width_pt, max(60.0, width * 0.75))
            para = doc.add_paragraph()
            try:
                run = para.add_run()
                run.add_picture(io.BytesIO(data), width=Pt(target_w))
                count += 1
            except Exception:
                # unsupported image format / decoder failure: skip silently
                continue
        return count


# Page emission ---------------------------------------------------------------

@dataclass
class _Item:
    """One page element in reading order (paragraph, table, or image)."""

    kind: str  # "paragraph" | "geo_table" | "pipe_table" | "columns" | "image"
    bbox: Any  # domain Rect
    payload: Any

    @property
    def y0(self) -> float:
        return self.bbox.y0

    @property
    def y1(self) -> float:
        return self.bbox.y1

    @property
    def x0(self) -> float:
        return self.bbox.x0

    @property
    def x1(self) -> float:
        return self.bbox.x1


def _emit_flow_paragraph(doc: Document, group: list[TextLine], left: float,
                         right: float, page_width: float,
                         next_item: _Item | None) -> None:
    """One Word paragraph from a merged line group, with inferred styling."""
    para = doc.add_paragraph()
    _quiet_paragraph(para)
    group_bbox = _union_bbox([ln.bbox for ln in group])
    if len(group) >= 2:
        size = _dominant_size(group)
        deltas = [b.baseline_y - a.baseline_y
                  for a, b in zip(group, group[1:], strict=False)]
        lead = median(deltas)
        if size > 0 and lead > 0:
            para.paragraph_format.line_spacing = _clamp(lead / size, 0.7, 3.0)
    align = _infer_alignment(group, left, right, page_width)
    para.alignment = align
    if align == WD_ALIGN_PARAGRAPH.LEFT:
        indent = group_bbox.x0 - left
        if 8.0 < indent < 200.0:
            para.paragraph_format.left_indent = Pt(round(indent, 1))
    if (next_item is not None and next_item.x0 < group_bbox.x1
            and next_item.x1 > group_bbox.x0):
        gap = next_item.y0 - group_bbox.y1
        if gap > 2.0:
            para.paragraph_format.space_after = Pt(round(min(gap, 24.0), 1))
    _emit_runs(para, group)


def _emit_image(doc: Document, data: bytes, bbox: Rect,
                max_width: float | None) -> bool:
    """Inline picture at its on-page size; False when the format is unusable."""
    para = doc.add_paragraph()
    _quiet_paragraph(para)
    w, h = max(1.0, bbox.width), max(1.0, bbox.height)
    if max_width and w > max_width:
        h *= max_width / w
        w = max_width
    try:
        run = para.add_run()
        run.add_picture(io.BytesIO(data), width=Pt(w), height=Pt(h))
        return True
    except Exception:
        para._p.getparent().remove(para._p)
        return False


def _emit_geometric_table(doc: Document, rows: list[list[Rect | None]],
                          page_lines: list[TextLine]) -> None:
    """Real Word table for a ruled PDF table; cell text from covered lines."""
    n_cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=n_cols)
    table.style = "Table Grid"
    for ri, cells in enumerate(rows):
        for ci, cell_rect in enumerate(cells):
            if ci >= n_cols:
                break
            cell = table.cell(ri, ci)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.TOP
            if cell_rect is None:
                continue
            cell_lines = [ln for ln in page_lines
                          if _line_center_in(ln.bbox, cell_rect, pad=1.0)]
            first = True
            for group in group_paragraphs(cell_lines):
                para = cell.paragraphs[0] if first else cell.add_paragraph()
                first = False
                _quiet_paragraph(para)
                _emit_runs(para, group)


def _emit_data_table(doc: Document, rows: list[list[list[TextRun]]]) -> None:
    """Real Word table from detected `|`-delimited rows."""
    if not rows:
        return
    n_cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=n_cols)
    table.style = "Table Grid"
    for ri, row in enumerate(rows):
        for ci, cell_runs in enumerate(row):
            if ci >= n_cols:
                break
            cell = table.cell(ri, ci)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.TOP
            para = cell.paragraphs[0]
            _quiet_paragraph(para)
            for run in cell_runs:
                if not run.text:
                    continue
                r = para.add_run(run.text)
                _apply_run_format(r, run.font)


def _emit_column_table(doc: Document, bands: list[list[TextLine]]) -> None:
    """Borderless table with one cell per detected column band."""
    if not bands:
        return
    table = doc.add_table(rows=1, cols=len(bands))
    # borderless: clear cell borders
    tbl = table._tbl
    tblPr = tbl.find(qn("w:tblPr"))
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        b = OxmlElement(f"w:{edge}")
        b.set(qn("w:val"), "nil")
        borders.append(b)
    tblPr.append(borders)
    for col_index, band in enumerate(bands):
        cell = table.cell(0, col_index)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.TOP
        # python-docx starts each cell with one empty paragraph; reuse it
        first = True
        for group in group_paragraphs(band):
            para = cell.paragraphs[0] if first else cell.add_paragraph()
            first = False
            _quiet_paragraph(para)
            _emit_runs(para, group)


_TABLE_KINDS = {"geo_table", "pipe_table", "columns"}


# Public entry point ---------------------------------------------------------

def write_docx(pymupdf_doc: pymupdf.Document, options: DocxExportOptions,
               dest: Path) -> DocxExportResult:
    """Convert `pymupdf_doc` into a DOCX at `dest`. Returns a `DocxExportResult`.

    The caller is responsible for atomic write/validation around this call
    (the worker does so via tempfile + reopen).
    """
    result = DocxExportResult()
    doc = Document()  # blank DOCX using python-docx's default template
    pages = list(pymupdf_doc)
    if not pages:
        raise DocxExportError("The document has no pages to export.")

    column_detector = ColumnDetector()
    table_detector = TableDetector()

    for index, page in enumerate(pages):
        # One Word section per PDF page: the section break itself paginates,
        # so no manual page breaks (and no blank pages between PDF pages).
        if index > 0:
            doc.add_section(WD_SECTION.NEW_PAGE)
            _quiet_paragraph(doc.paragraphs[-1])  # the section-break paragraph
        section = doc.sections[-1]

        lines = extract_page_lines(page, with_chars=False)
        horizontal = [ln for ln in lines if ln.direction == Direction.HORIZONTAL]
        if len(horizontal) != len(lines):
            result.unsupported_items.append(UnsupportedItem(
                page_index=index, kind="vertical_text",
                message="Vertical text was skipped — Word cannot flow vertical "
                        "text."))

        # 1. Ruled tables claim their lines first so cells are not duplicated
        #    as free-flowing paragraphs.
        geo_tables = _find_geometric_tables(page) if options.detect_tables else []
        table_bboxes = [bbox for bbox, _rows in geo_tables]
        flow_lines = [
            ln for ln in horizontal
            if not any(_line_center_in(ln.bbox, tb) for tb in table_bboxes)
        ]

        images = _page_image_items(page) if options.embed_images else []

        # 2. Fit section margins (and alignment edges) to the page content.
        geom = page_geometry(page)
        content = [ln.bbox for ln in flow_lines]
        content += [bbox for bbox, _rows in geo_tables]
        content += [bbox for bbox, _data in images]
        margins = _content_margins(geom, content)
        _configure_section(section, page, margins)
        left_edge = margins[0]
        right_edge = geom.width - margins[1]
        usable_width = max(36.0, right_edge - left_edge)

        items: list[_Item] = []
        for bbox, rows in geo_tables:
            items.append(_Item("geo_table", bbox, rows))
        for bbox, data in images:
            items.append(_Item("image", bbox, data))

        # 3. Pipe-delimited table fallback; consumed lines leave the flow.
        detailed = table_detector.detect_detailed(flow_lines) \
            if options.detect_tables else None
        if detailed is not None:
            rows, kept = detailed
            consumed = {id(ln) for ln in kept}
            flow_lines = [ln for ln in flow_lines if id(ln) not in consumed]
            items.append(_Item("pipe_table", _union_bbox([ln.bbox for ln in kept]),
                               rows))

        # 4. Remaining lines: columns become a borderless layout table,
        #    otherwise merged paragraphs.
        bands = (column_detector.detect(flow_lines) if options.detect_columns
                 else [flow_lines])
        if len(bands) >= 2 and flow_lines:
            items.append(_Item("columns", _union_bbox([ln.bbox for ln in flow_lines]),
                               bands))
        else:
            for group in group_paragraphs(flow_lines):
                items.append(_Item(
                    "paragraph", _union_bbox([ln.bbox for ln in group]), group))

        # 5. Emit in reading order.
        items.sort(key=lambda it: (round(it.y0, 1), it.x0))
        last_kind = None
        for i, item in enumerate(items):
            if item.kind in _TABLE_KINDS and last_kind in _TABLE_KINDS:
                # adjacent OOXML tables fuse in Word; keep them apart
                spacer = doc.add_paragraph()
                _quiet_paragraph(spacer)
            if item.kind == "paragraph":
                nxt = items[i + 1] if i + 1 < len(items) else None
                _emit_flow_paragraph(doc, item.payload, left_edge, right_edge,
                                     geom.width, nxt)
            elif item.kind == "image":
                if not _emit_image(doc, item.payload, item.bbox, usable_width):
                    result.warnings.append(
                        f"Page {index + 1}: an image used an unsupported format "
                        f"and was skipped.")
            elif item.kind == "geo_table":
                _emit_geometric_table(doc, item.payload, horizontal)
            elif item.kind == "pipe_table":
                _emit_data_table(doc, item.payload)
            elif item.kind == "columns":
                _emit_column_table(doc, item.payload)
            last_kind = item.kind
        result.pages_written += 1

    # Save to a temporary file first so the caller (or this function) can
    # validate it before it lands at the user's chosen path. We save in-place
    # here; the worker wraps this in tempfile + reopen validation.
    doc.save(str(dest))
    result.ok = True
    result.output_path = str(dest)
    return result


def write_docx_atomic(pymupdf_doc: pymupdf.Document, options: DocxExportOptions,
                      dest: Path) -> DocxExportResult:
    """Atomic write: tempfile -> reopen-validate -> os.replace(dest)."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DocxExportError(f"Cannot create destination folder: {exc}") from exc

    fd, tmp_name = tempfile.mkstemp(suffix=".docx", dir=str(dest.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        result = write_docx(pymupdf_doc, options, tmp_path)
        # Reopen + sanity check.
        try:
            reopened = Document(str(tmp_path))
            _ = len(reopened.paragraphs)
        except Exception as exc:
            raise DocxExportError(
                f"The output DOCX failed to reopen: {exc}") from exc
        try:
            os.replace(tmp_path, dest)
        except OSError as exc:
            raise DocxExportError(f"Could not save the DOCX: {exc}") from exc
        result.output_path = str(dest)
        return result
    except Exception:
        # Clean up the temp file on any failure; re-raise for the worker.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
