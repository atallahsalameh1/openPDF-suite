"""Round-trip tests for the coordinate service (AGENTS.md §8)."""

from __future__ import annotations

import pytest

from openpdfsuite.domain.coordinates import CoordinateService, PageTransform, ViewTransform
from openpdfsuite.domain.models import Point, Rect


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_page_transform_roundtrip(rotation):
    t = PageTransform(crop_x0=100, crop_y0=80, crop_w=550, crop_h=770, rotation=rotation)
    for p in (Point(100, 80), Point(375, 465), Point(649.9, 849.9), Point(150.5, 200.25)):
        page = t.cropped_to_page(t.to_cropped(p))
        back = t.to_user(page)
        assert abs(back.x - p.x) < 1e-6
        assert abs(back.y - p.y) < 1e-6


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_page_dimensions_swap_under_rotation(rotation):
    t = PageTransform(crop_x0=0, crop_y0=0, crop_w=200, crop_h=400, rotation=rotation)
    if rotation in (90, 270):
        assert t.page_width == 400 and t.page_height == 200
    else:
        assert t.page_width == 200 and t.page_height == 400


@pytest.mark.parametrize("zoom", [0.5, 1.0, 2.37])
@pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
def test_view_roundtrip(zoom, dpr):
    page = PageTransform(crop_x0=36, crop_y0=12, crop_w=523, crop_h=790, rotation=0)
    view = ViewTransform(zoom=zoom, scroll_x=137.3, scroll_y=921.7,
                         page_origin_x=40, page_origin_y=16, dpr=dpr)
    svc = CoordinateService(page, view)
    for p in (Point(100, 200), Point(36.5, 12.25), Point(500, 700)):
        v = svc.user_to_view(p)
        back = svc.view_to_user(v)
        assert abs(back.x - p.x) < 1e-6
        assert abs(back.y - p.y) < 1e-6
    # device pixel roundtrip
    dv = view.view_to_device(Point(10, 20))
    back = view.device_to_view(dv)
    assert abs(back.x - 10) < 1e-9 and abs(back.y - 20) < 1e-9


def test_rect_rotation_maps_to_axis_aligned():
    t = PageTransform(crop_x0=0, crop_y0=0, crop_w=200, crop_h=400, rotation=90)
    r = Rect(10, 20, 60, 80)
    rp = t.rect_to_page(r)
    back = t.rect_to_user(rp)
    assert abs(back.x0 - r.x0) < 1e-6
    assert abs(back.y1 - r.y1) < 1e-6
    assert rp.width == pytest.approx(r.height, abs=1e-6)
    assert rp.height == pytest.approx(r.width, abs=1e-6)
