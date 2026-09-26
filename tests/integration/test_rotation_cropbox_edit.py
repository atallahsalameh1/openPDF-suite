"""Rotated (/Rotate) and cropbox pages: the full edit path must place text
where the old text visually was (AGENTS.md §8 coordinate round trip).

Engine geometry is unrotated crop-normal space; pixmaps render the rotated
display space. These tests assert the display-space placement of replacements
via `page.rotation_matrix`, which is exactly what the on-screen user sees.
"""

from __future__ import annotations

import pymupdf

from openpdfsuite.domain.models import EditMode, ReplacementEdit
from openpdfsuite.infrastructure.pdf.editor import PdfEditEngine
from openpdfsuite.infrastructure.pdf.extractor import build_regions


def _line_region(doc: pymupdf.Document, needle: str):
    page = doc[0]
    regions = build_regions(page, "d", 0)
    return next(r for r in regions
                if r.mode == EditMode.PRESERVE_LINE and needle in r.text)


def _display_rect(page: pymupdf.Page, rect_tuple) -> pymupdf.Rect:
    return pymupdf.Rect(*rect_tuple) * page.rotation_matrix


def test_rotated_page_mode_a_replacement_places_text_at_old_spot(rotated_pdf):
    doc = pymupdf.open(str(rotated_pdf))
    engine = PdfEditEngine(doc, "d")
    page = doc[0]
    target = _line_region(doc, "Rotated page headline.")
    old_disp = _display_rect(page, target.bbox.as_tuple())

    edit = ReplacementEdit(region_id=target.region_id, source_revision=0,
                           new_text="REPLACED HEADLINE XYZ",
                           mode=EditMode.PRESERVE_LINE, page_index=0)
    prepared = engine.prepare(0, target, edit)
    assert prepared.ok, prepared.issues
    assert prepared.validation.ok

    cand = pymupdf.open("pdf", prepared.candidate_bytes)
    text = cand[0].get_text()
    assert "Rotated page headline." not in text, "old text must be removed"
    assert "REPLACED HEADLINE XYZ" in text
    new_disp = cand[0].search_for("REPLACED HEADLINE XYZ")[0] * cand[0].rotation_matrix
    assert abs(new_disp.x0 - old_disp.x0) < 2.0, "replacement moved horizontally"
    assert abs(new_disp.y0 - old_disp.y0) < 2.0, "replacement moved vertically"
    cand.close()
    doc.close()


def test_cropbox_page_mode_a_replacement_places_text_at_old_spot(fixture_dir):
    path = fixture_dir / "cropbox.pdf"
    doc = pymupdf.open(str(path))
    engine = PdfEditEngine(doc, "d")
    page = doc[0]
    target = _line_region(doc, "Cropbox offset headline.")
    old_disp = _display_rect(page, target.bbox.as_tuple())

    edit = ReplacementEdit(region_id=target.region_id, source_revision=0,
                           new_text="REPLACED HEADLINE XYZ",
                           mode=EditMode.PRESERVE_LINE, page_index=0)
    prepared = engine.prepare(0, target, edit)
    assert prepared.ok, prepared.issues

    cand = pymupdf.open("pdf", prepared.candidate_bytes)
    text = cand[0].get_text()
    assert "Cropbox offset headline." not in text
    assert "REPLACED HEADLINE XYZ" in text
    new_disp = cand[0].search_for("REPLACED HEADLINE XYZ")[0] * cand[0].rotation_matrix
    assert abs(new_disp.x0 - old_disp.x0) < 2.0
    assert abs(new_disp.y0 - old_disp.y0) < 2.0
    cand.close()
    doc.close()


def test_rotated_page_prepare_insert_validates_pixels(rotated_pdf):
    """Add-text on a rotated page: the pixel-validation mask must be in
    display space (regression: correct inserts were rejected as 'unexpected
    visual change outside the region')."""
    doc = pymupdf.open(str(rotated_pdf))
    engine = PdfEditEngine(doc, "d")
    # an empty engine-space area (rotated page renders it elsewhere)
    from openpdfsuite.domain.models import Rect
    box = Rect(50.0, 400.0, 250.0, 460.0)
    edit = ReplacementEdit(region_id="insert:p0:r0", source_revision=0,
                           new_text="Inserted on a rotated page.",
                           mode=EditMode.REFLOW_BOX, page_index=0,
                           target_box=None)
    prepared = engine.prepare_insert(0, box, edit)
    assert prepared.ok, prepared.issues
    assert prepared.validation.ok

    cand = pymupdf.open("pdf", prepared.candidate_bytes)
    assert "Inserted on a rotated page." in cand[0].get_text()
    cand.close()
    doc.close()
