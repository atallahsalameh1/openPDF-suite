"""View-level coordinate conversions (AGENTS.md §8).

Regression: on /Rotate pages the canvas renders the rotated page while region
geometry arrives unrotated. Hover/selection/hit-testing used to compare mouse
(display space) against engine bboxes, so outlines drew at the wrong end of
the page and clicks missed the text.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from PySide6.QtCore import QPoint

from openpdfsuite.domain.models import Rect
from openpdfsuite.infrastructure.pdf.extractor import build_regions
from openpdfsuite.ui.document_view import PAGE_MARGIN, DocumentView


@pytest.fixture()
def rotated_view(qtbot, theme):
    """A view showing the rotated fixture page (display 842x595, /Rotate 90)."""
    doc = pymupdf.open(str(Path(__file__).parents[1] / "fixtures" / "generated"
                           / "rotated.pdf"))
    view = DocumentView(theme)
    qtbot.addWidget(view)
    view.set_document([(doc[0].rect.width, doc[0].rect.height)],
                      rotations=[doc[0].rotation])
    yield view, doc
    doc.close()


def test_engine_rect_maps_to_display_rect(rotated_view):
    view, _doc = rotated_view
    # empirical anchor: redacting engine rect (72,85,225,104) changes pixels
    # at display x~738-757, y~72-225 (measured with a render diff)
    disp = view.rect_to_display(0, Rect(72.0, 85.0, 225.3, 104.3))
    assert disp.x0 == pytest.approx(737.7, abs=0.5)
    assert disp.x1 == pytest.approx(757.0, abs=0.5)
    assert disp.y0 == pytest.approx(72.0, abs=0.5)
    assert disp.y1 == pytest.approx(225.3, abs=0.5)


def test_engine_display_round_trip(rotated_view):
    view, _doc = rotated_view
    engine_rect = Rect(72.0, 85.0, 225.3, 104.3)
    back = view.rect_to_engine(0, view.rect_to_display(0, engine_rect))
    for a, b in zip(engine_rect.as_tuple(), back.as_tuple(), strict=True):
        assert a == pytest.approx(b, abs=0.001)


def test_hit_test_finds_region_at_display_position(rotated_view):
    """Clicking where the text VISUALLY is must return its region (the bug:
    clicks at the rotated location missed because bboxes were unrotated)."""
    view, doc = rotated_view
    regions = build_regions(doc[0], "d", 0)
    view.page_regions[0] = regions
    target = next(r for r in regions if "Rotated page headline." in r.text)

    disp = view.rect_to_display(0, target.bbox)
    cx = (disp.x0 + disp.x1) / 2
    cy = (disp.y0 + disp.y1) / 2
    frame = view.frames[0]
    pos = QPoint(int(PAGE_MARGIN + cx * frame.zoom),
                 int(PAGE_MARGIN + cy * frame.zoom))
    found = view._region_at(frame, pos)
    assert found is not None and found.region_id == target.region_id, \
        "click on visually-rotated text must hit its region"


def test_hit_test_misses_when_clicking_the_wrong_spot(rotated_view):
    """Guard: the hit test is not vacuous — a display point far from the text
    (engine space equivalent) must not return the region."""
    view, doc = rotated_view
    regions = build_regions(doc[0], "d", 0)
    view.page_regions[0] = regions

    frame = view.frames[0]
    # display top-left corner area; engine text sits elsewhere
    pos = QPoint(int(PAGE_MARGIN + 30), int(PAGE_MARGIN + 30))
    found = view._region_at(frame, pos)
    assert found is None or "Rotated page headline." not in found.text


def test_hit_test_without_rotation_data_still_works(qtbot, theme):
    """Backwards compatibility: no rotations payload -> identity transform."""
    view = DocumentView(theme)
    qtbot.addWidget(view)
    view.set_document([(595.0, 842.0)])  # no rotations argument
    assert view.page_rotations == []
    disp = view.rect_to_display(0, Rect(10, 20, 30, 40))
    assert disp.as_tuple() == (10.0, 20.0, 30.0, 40.0)
