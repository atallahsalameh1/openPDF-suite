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
