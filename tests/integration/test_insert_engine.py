"""M4 engine tests: pure insertion (Add Text) + Mode B target-box overlap guard."""

from __future__ import annotations

import pymupdf

from openpdfsuite.domain.models import EditMode, Rect, ReplacementEdit
from openpdfsuite.infrastructure.pdf.editor import PdfEditEngine
from openpdfsuite.infrastructure.pdf.extractor import build_regions
from tests.fixtures.make_fixtures import PARA_LINE1, TITLE

# standard.pdf page 0: title ~60-86, paragraph ~131-194, trailing ~235-250;
# the area below 260 pt is empty
EMPTY_BOX = (72.0, 300.0, 400.0, 348.0)


def _engine(fixture_dir) -> PdfEditEngine:
    return PdfEditEngine(pymupdf.open(str(fixture_dir / "standard.pdf")), "d")


def _insert_edit(text="Hello added text", box=EMPTY_BOX, **kw):
    return ReplacementEdit(
        region_id="insert-test", source_revision=0, new_text=text,
        mode=EditMode.REFLOW_BOX, target_box=Rect(*box), page_index=0, **kw)


def test_insert_success(fixture_dir):
    eng = _engine(fixture_dir)
    prepared = eng.prepare_insert(0, Rect(*EMPTY_BOX), _insert_edit("Hello added text"))
    assert prepared.ok, prepared.issues
    rev = eng.commit(prepared)
    assert rev == 1
    text = eng.doc[0].get_text()
    assert "Hello added text" in text
    assert TITLE in text  # nothing else touched


def test_insert_refuses_existing_text(fixture_dir):
    eng = _engine(fixture_dir)
    box = (72.0, 62.0, 400.0, 92.0)  # right over the title
    prepared = eng.prepare_insert(0, Rect(*box), _insert_edit("collide", box=box))
    assert not prepared.ok
    assert any("already has text" in i for i in prepared.issues)


def test_insert_overflow_reports_required_size(fixture_dir):
    eng = _engine(fixture_dir)
    prepared = eng.prepare_insert(0, Rect(*EMPTY_BOX), _insert_edit("word " * 60))
    assert not prepared.ok
    assert prepared.validation.overflowed
    assert prepared.validation.required_size < 11.0


def test_insert_auto_shrink(fixture_dir):
    eng = _engine(fixture_dir)
    prepared = eng.prepare_insert(
        0, Rect(*EMPTY_BOX), _insert_edit("word " * 46, auto_shrink=True))
    assert prepared.ok, prepared.issues
    assert any("auto-shrunk" in i for i in prepared.validation.issues)
    eng.commit(prepared)
    assert "word" in eng.doc[0].get_text()


def test_insert_undo_via_pre_bytes(fixture_dir):
    eng = _engine(fixture_dir)
    prepared = eng.prepare_insert(0, Rect(*EMPTY_BOX), _insert_edit())
    assert prepared.ok
    pre = prepared.pre_bytes
    eng.commit(prepared)
    assert "Hello added text" in eng.doc[0].get_text()
    restored = pymupdf.open(stream=pre)
    assert "Hello added text" not in restored[0].get_text()
    restored.close()


def test_mode_b_target_overlapping_foreign_text_refused(fixture_dir):
    """Resizing the reflow box onto the trailing line must be rejected (§9)."""
    eng = _engine(fixture_dir)
    regions = build_regions(eng.doc[0], "d", 0)
    para = next(r for r in regions if r.mode == EditMode.REFLOW_BOX
                and PARA_LINE1 in r.text)
    box = (para.bbox.x0, para.bbox.y0, para.bbox.x1, 260.0)  # reaches TRAILING
    edit = ReplacementEdit(
        region_id=para.region_id, source_revision=0, new_text="replaced paragraph",
        mode=EditMode.REFLOW_BOX, target_box=Rect(*box), page_index=0)
    prepared = eng.prepare(0, para, edit)
    assert not prepared.ok
    assert any("overlaps other text" in i for i in prepared.issues)


def test_mode_b_resized_box_within_gap_accepted(fixture_dir):
    """Growing the box into the empty gap between paragraph and trailing line
    is fine: longer text fits without touching the neighbor."""
    eng = _engine(fixture_dir)
    regions = build_regions(eng.doc[0], "d", 0)
    para = next(r for r in regions if r.mode == EditMode.REFLOW_BOX
                and PARA_LINE1 in r.text)
    box = (para.bbox.x0, para.bbox.y0, para.bbox.x1, 228.0)  # stays above ~235
    edit = ReplacementEdit(
        region_id=para.region_id, source_revision=0, new_text="word " * 44,
        mode=EditMode.REFLOW_BOX, target_box=Rect(*box), page_index=0)
    prepared = eng.prepare(0, para, edit)
    assert prepared.ok, prepared.issues
    eng.commit(prepared)
    text = eng.doc[0].get_text()
    assert PARA_LINE1 not in text
    assert "End of first section." in text  # neighbor intact
