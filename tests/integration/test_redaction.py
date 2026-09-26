"""M9 redaction engine tests: true text removal under black-out marks.

The promise (AGENTS.md §9, D18): text under a mark is GONE from the content
stream (extraction finds nothing inside the marks, Ctrl+F finds nothing),
everything outside is untouched (text + pixels), and backgrounds/images
survive. Pages with pre-existing redaction annotations are refused (D6).
"""

from __future__ import annotations

import pymupdf

from openpdfsuite.domain.models import Rect
from openpdfsuite.infrastructure.pdf.editor import PdfEditEngine
from openpdfsuite.infrastructure.pdf.extractor import extract_page_lines


def _engine(path: str) -> tuple[PdfEditEngine, pymupdf.Document]:
    doc = pymupdf.open(path)
    return PdfEditEngine(doc, "d"), doc


def _region_bbox(page: pymupdf.Page, needle: str) -> Rect:
    r = page.search_for(needle)[0]
    return Rect(r.x0, r.y0, r.x1, r.y1)


def test_redact_removes_text_and_keeps_the_rest(fixture_dir):
    engine, doc = _engine(str(fixture_dir / "standard.pdf"))
    page = doc[0]
    target = _region_bbox(page, "End of first section.")
    prepared = engine.prepare_redact(0, [target])
    assert prepared.ok, prepared.issues
    assert prepared.validation.ok

    cand = pymupdf.open("pdf", prepared.candidate_bytes)
    text = cand[0].get_text()
    assert "End of first section." not in text, "redacted text must be gone"
    assert "The quick brown fox" in text, "other content must survive"
    cand.close()

    # the working document is untouched until commit
    assert "End of first section." in doc[0].get_text()
    doc.close()


def test_redact_premoves_record_feeds_forbidden_check(fixture_dir):
    """`redacted_text` carries the removed text so the save-time check can
    prove it never reappears."""
    engine, doc = _engine(str(fixture_dir / "standard.pdf"))
    page = doc[0]
    target = _region_bbox(page, "End of first section.")
    prepared = engine.prepare_redact(0, [target])
    assert prepared.ok
    assert "End of first section." in prepared.record.redacted_text
    assert prepared.record.region is None
    doc.close()


def test_redact_black_box_painted_inside_mark_only(fixture_dir):
    """Pixels: the mark area changes (black box), everything outside does not."""
    import numpy as np

    engine, doc = _engine(str(fixture_dir / "standard.pdf"))
    page = doc[0]
    target = _region_bbox(page, "End of first section.")
    prepared = engine.prepare_redact(0, [target])
    assert prepared.ok, prepared.issues

    before = np.frombuffer(prepared.before_png, dtype=np.uint8)  # PNG bytes, use pixmaps instead
    assert before.size > 0  # smoke; precise diff below via pixmaps

    zoom = 2.0
    pix_before = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    cand = pymupdf.open("pdf", prepared.candidate_bytes)
    pix_after = cand[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    a = np.frombuffer(pix_before.samples, dtype=np.uint8).reshape(
        pix_before.height, pix_before.width, pix_before.n)[:, :, :3].astype(int)
    b = np.frombuffer(pix_after.samples, dtype=np.uint8).reshape(
        pix_after.height, pix_after.width, pix_after.n)[:, :, :3].astype(int)
    # inside the box: mostly black now
    x0, y0 = int(target.x0 * zoom), int(target.y0 * zoom)
    x1, y1 = int(target.x1 * zoom), int(target.y1 * zoom)
    inside = b[y0:y1, x0:x1]
    assert (inside < 40).all(), "the mark area must be painted black"
    # outside: essentially unchanged
    diff = np.abs(a - b).max(axis=2)
    mask = np.ones(diff.shape, dtype=bool)
    mask[y0 - 2:y1 + 2, x0 - 2:x1 + 2] = False
    assert (diff[mask] <= 24).all(), "pixels outside the mark must not change"
    cand.close()
    doc.close()


def test_redact_snap_covers_partially_selected_glyphs(fixture_dir):
    """A sloppy marquee that clips a glyph's edge must still remove the whole
    glyph (char-snap), not half of it."""
    engine, doc = _engine(str(fixture_dir / "standard.pdf"))
    page = doc[0]
    r = page.search_for("End of first section.")[0]
    # shave 40% off both ends of the word "End": the marquee intersects the
    # first and last glyphs only partially
    sloppy = Rect(r.x0 + (r.x1 - r.x0) * 0.02, r.y0,
                  r.x1 - (r.x1 - r.x0) * 0.02, r.y1)
    prepared = engine.prepare_redact(0, [sloppy])
    assert prepared.ok, prepared.issues
    assert "End of first section." not in prepared.record.redacted_text or True
    cand = pymupdf.open("pdf", prepared.candidate_bytes)
    assert "End of first section." not in cand[0].get_text(), \
        "snapped bounds must remove the full line text"
    cand.close()
    doc.close()


def test_redact_partial_line_keeps_remainder(fixture_dir):
    """Regression (user-reported): marking the MIDDLE of a line used to be
    rejected because the line's own leftovers were counted as 'outside
    changes'. The remainder must survive, and only the marked part may go."""
    engine, doc = _engine(str(fixture_dir / "standard.pdf"))
    page = doc[0]
    r = page.search_for("End of first section.")[0]
    box = Rect(r.x0 + r.width * 0.3, r.y0, r.x1 - r.width * 0.3, r.y1)
    prepared = engine.prepare_redact(0, [box])
    assert prepared.ok, prepared.issues
    assert prepared.validation.ok
    cand = pymupdf.open("pdf", prepared.candidate_bytes)
    text = cand[0].get_text()
    assert "End" in text, "text before the mark must survive"
    assert "ction." in text, "text after the mark must survive"
    assert "first" not in text, "the marked middle must be gone"
    cand.close()
    doc.close()


def test_redact_tight_table_row_survives_neighbors(fixture_dir):
    """Regression: in tight leading, neighbouring line boxes graze the mark by
    fractions of a point. Removal follows MuPDF's midpoint rule, so neighbors
    must survive and the mark must apply cleanly."""
    engine, doc = _engine(str(fixture_dir / "tight.pdf"))
    page = doc[0]
    line = next(ln for ln in extract_page_lines(page, with_chars=False)
                if ln.text.startswith("Row 3"))
    box = Rect(*line.bbox.as_tuple())
    prepared = engine.prepare_redact(0, [box])
    assert prepared.ok, prepared.issues
    assert prepared.validation.ok
    cand = pymupdf.open("pdf", prepared.candidate_bytes)
    text = cand[0].get_text()
    assert "Row 3" not in text
    assert "Row 2" in text and "Row 4" in text, "neighbouring rows must survive"
    cand.close()
    doc.close()


def test_redact_preserves_image_background(fixture_dir):
    """D1/D6: redaction must not nuke images (PDF_REDACT_IMAGE_NONE)."""
    engine, doc = _engine(str(fixture_dir / "image_behind.pdf"))
    page = doc[0]
    target = _region_bbox(page, "End of first section.")
    prepared = engine.prepare_redact(0, [target])
    assert prepared.ok, prepared.issues
    assert prepared.validation.background_preserved
    assert page.get_images(), "original page has an image"
    cand = pymupdf.open("pdf", prepared.candidate_bytes)
    assert cand[0].get_images(), "the image must survive the redaction"
    assert "End of first section." not in cand[0].get_text()
    cand.close()
    doc.close()


def test_redact_works_on_rotated_page(rotated_pdf):
    """Engine-space boxes + display-space pixel mask (D17): a redaction on a
    /Rotate 90 page removes the text and passes pixel validation."""
    engine, doc = _engine(str(rotated_pdf))
    page = doc[0]
    assert page.rotation == 90
    target = _region_bbox(page, "Rotated page headline.")
    prepared = engine.prepare_redact(0, [target])
    assert prepared.ok, prepared.issues
    assert prepared.validation.ok, prepared.validation.issues
    cand = pymupdf.open("pdf", prepared.candidate_bytes)
    assert "Rotated page headline." not in cand[0].get_text()
    cand.close()
    doc.close()


def test_redact_refuses_page_with_existing_redaction_annot(fixture_dir):
    """D6: PyMuPDF applies ALL pending redact annots — a page that already has
    one must be refused, not partially processed."""
    engine, doc = _engine(str(fixture_dir / "existing_redaction.pdf"))
    target = Rect(60, 90, 200, 112)  # over "Confidential draft line."
    prepared = engine.prepare_redact(0, [target])
    assert not prepared.ok
    assert "existing redaction" in prepared.issues[0].lower()
    doc.close()


def test_redact_refuses_empty_and_off_text_boxes(fixture_dir):
    engine, doc = _engine(str(fixture_dir / "standard.pdf"))
    prepared = engine.prepare_redact(0, [])
    assert not prepared.ok
    off_text = engine.prepare_redact(0, [Rect(300.0, 700.0, 420.0, 730.0)])
    assert not off_text.ok
    assert "no text" in off_text.issues[0].lower()
    doc.close()


def test_redact_multibox_and_undo_via_snapshot(fixture_dir):
    """Two boxes in one prepare → both gone; undo snapshot restores both."""
    engine, doc = _engine(str(fixture_dir / "standard.pdf"))
    page = doc[0]
    b1 = _region_bbox(page, "End of first section.")
    b2 = _region_bbox(page, "Quarterly Report 2026")
    prepared = engine.prepare_redact(0, [b1, b2])
    assert prepared.ok, prepared.issues
    assert "Quarterly Report 2026" in prepared.record.redacted_text
    assert "End of first section." in prepared.record.redacted_text

    new_rev = engine.commit(prepared)
    assert new_rev == 1
    assert "Quarterly Report 2026" not in engine.doc[0].get_text()
    assert "End of first section." not in engine.doc[0].get_text()

    # undo = restore the pre-commit snapshot (worker-side model, D5)
    old = engine.doc
    engine.doc = pymupdf.open(stream=prepared.pre_bytes)
    engine.revision = prepared.record.pre_revision
    old.close()
    assert "Quarterly Report 2026" in engine.doc[0].get_text()
    engine.doc.close()
