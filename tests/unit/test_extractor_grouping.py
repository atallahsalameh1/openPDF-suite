"""Conservative paragraph grouping (AGENTS.md §8): columns and table-like
gaps must never merge; real paragraphs must."""

from __future__ import annotations

import pymupdf
import pytest

from openpdfsuite.domain.models import EditMode
from openpdfsuite.infrastructure.pdf.extractor import (
    build_regions,
    extract_page_lines,
    group_paragraphs,
)
from tests.fixtures.make_fixtures import (
    WRAPPED_AFTER,
    WRAPPED_CELL_R1,
    WRAPPED_CELL_R2_A,
    WRAPPED_CELL_R2_B,
    WRAPPED_CELL_R3,
)


@pytest.fixture()
def para_doc():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 100
    for line in ("First paragraph line one.", "First paragraph line two.",
                 "First paragraph line three."):
        page.insert_text((72, y), line, fontsize=11, fontname="helv")
        y += 15
    y += 40  # clear gap
    page.insert_text((72, y), "Isolated heading after gap.", fontsize=11, fontname="hebo")
    yield doc
    doc.close()


def test_paragraph_groups_tight_lines(para_doc):
    lines = extract_page_lines(para_doc[0])
    groups = group_paragraphs(lines)
    big = [g for g in groups if len(g) == 3]
    assert len(big) == 1, f"expected one 3-line paragraph, got {[len(g) for g in groups]}"
    texts = [ln.text for ln in big[0]]
    assert all("First paragraph" in t for t in texts)


def test_gap_prevents_merge(para_doc):
    lines = extract_page_lines(para_doc[0])
    groups = group_paragraphs(lines)
    for g in groups:
        assert not any("Isolated" in ln.text and "First" in ln2.text
                       for ln in g for ln2 in g)
    isolated = [g for g in groups if any("Isolated" in ln.text for ln in g)]
    assert isolated and len(isolated[0]) == 1


def test_font_change_prevents_merge(para_doc):
    # the isolated line is bold (hebo) — grouping must not attach it above
    lines = extract_page_lines(para_doc[0])
    groups = group_paragraphs(lines)
    assert max(len(g) for g in groups) == 3


def test_columns_do_not_merge(fixture_dir):
    doc = pymupdf.open(str(fixture_dir / "columns.pdf"))
    lines = extract_page_lines(doc[0])
    groups = group_paragraphs(lines)
    for g in groups:
        texts = " ".join(ln.text for ln in g)
        assert not ("Left column" in texts and "Right column" in texts), \
            "columns merged into one paragraph"
    doc.close()


def test_table_rows_stay_line_level(fixture_dir):
    doc = pymupdf.open(str(fixture_dir / "tight.pdf"))
    # monospace table rows with '|' separators: tight leading but each row is
    # its own line; grouping may merge them as a paragraph only if fonts match —
    # what must hold is that every row remains individually addressable as a
    # PRESERVE_LINE region
    regions = build_regions(doc[0], "d", 0)
    line_regions = [r for r in regions if r.mode == EditMode.PRESERVE_LINE]
    row_texts = {r.text.strip() for r in line_regions}
    for i in range(1, 7):
        assert any(f"Row {i} " in t for t in row_texts)
    doc.close()


def test_region_ids_are_revision_scoped(para_doc):
    r0 = build_regions(para_doc[0], "docA", 0)
    r1 = build_regions(para_doc[0], "docA", 1)
    assert r0[0].region_id != r1[0].region_id
    assert ":r0:" in r0[0].region_id and ":r1:" in r1[0].region_id


def test_region_ids_unique_per_page(para_doc):
    regions = build_regions(para_doc[0], "docA", 0)
    ids = [r.region_id for r in regions]
    assert len(ids) == len(set(ids)), "region ids must be unique"


# -- table rows must not collapse into one paragraph -------------------------


def test_identical_table_rows_do_not_merge(wrapped_table_pdf):
    """Regression: repeating cells + even spacing used to merge every row.

    Columns 1-2 repeat the same text and the rows are evenly spaced, so each
    consecutive pair passed the pair-local gap/overlap/font tests and the whole
    table became one giant REFLOW_BOX.
    """
    doc = pymupdf.open(str(wrapped_table_pdf))
    regions = build_regions(doc[0], "d", 0)
    paras = [r for r in regions if r.mode == EditMode.REFLOW_BOX]
    for para in paras:
        text = para.text
        # no paragraph may span two different data rows
        assert not (WRAPPED_CELL_R1 in text and WRAPPED_CELL_R2_A in text), \
            "row 1 and row 2 merged into one region"
        assert not (WRAPPED_CELL_R2_A in text and WRAPPED_CELL_R3 in text), \
            "row 2 and row 3 merged into one region"
        assert WRAPPED_AFTER not in text, "trailing paragraph merged into the table"
    doc.close()


def test_wrapped_cell_lines_still_merge(wrapped_table_pdf):
    """The fix must not over-reject: a cell wrapping onto two lines is a
    genuine paragraph and still groups."""
    doc = pymupdf.open(str(wrapped_table_pdf))
    regions = build_regions(doc[0], "d", 0)
    paras = [r for r in regions if r.mode == EditMode.REFLOW_BOX]
    wrapped = [r for r in paras if WRAPPED_CELL_R2_A in r.text]
    assert wrapped, "the wrapped cell lines should still form a paragraph"
    assert WRAPPED_CELL_R2_B in wrapped[0].text, "both wrapped lines must be grouped"
    doc.close()


def test_table_lines_carry_grouping_reason(wrapped_table_pdf):
    doc = pymupdf.open(str(wrapped_table_pdf))
    regions = build_regions(doc[0], "d", 0)
    by_text = {r.text.strip(): r for r in regions
               if r.mode == EditMode.PRESERVE_LINE}
    row = by_text[WRAPPED_CELL_R3]
    assert row.grouping_reason, "table-lines should explain why they are line-level"
    # a plain paragraph line elsewhere is not flagged
    plain = by_text[WRAPPED_AFTER]
    assert plain.grouping_reason == ""
    doc.close()


def test_other_column_text_blocks_merge():
    """Synthetic minimal case: third line sitting in the gap blocks the merge."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    # two candidates at x=72, gap of 4pt
    page.insert_text((72, 100), "Alpha text on the first row.", fontsize=11, fontname="helv")
    page.insert_text((72, 115), "Beta text on the second row.", fontsize=11, fontname="helv")
    # a third line in the gap, inside the candidates' x-range
    page.insert_text((90, 108), "neighbour", fontsize=9, fontname="helv")
    lines = extract_page_lines(page)
    groups = group_paragraphs(lines)
    merged = [g for g in groups if len(g) == 2]
    assert not merged, "a line in the gap must block the merge"
    doc.close()


def test_long_paragraph_still_merges():
    """Guard against over-correction: a real 6-line paragraph with an empty
    right margin must still group (and survive the new blocker scan)."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 100
    for i in range(6):
        page.insert_text((72, y), f"Paragraph body line number {i + 1}.", fontsize=11,
                         fontname="helv")
        y += 15
    lines = extract_page_lines(page)
    groups = group_paragraphs(lines)
    assert max(len(g) for g in groups) == 6, \
        f"expected one 6-line paragraph, got {[len(g) for g in groups]}"
    doc.close()
