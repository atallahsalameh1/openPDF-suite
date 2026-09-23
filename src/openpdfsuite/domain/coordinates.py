"""Central coordinate transformation service (AGENTS.md §8).

One service converts between:
  * PDF user space (unrotated page coords, crop box offset applied)
  * page space as rendered (rotation applied)
  * view space (zoom + scroll, Qt logical pixels)
  * device pixels (logical px * device pixel ratio)

Every consumer — rendering placement, hit testing, overlays, selection
geometry — must use these functions so behavior cannot drift apart.

Conventions
-----------
PDF user space: origin at MediaBox top-left *after* crop offset subtraction,
y down (PyMuPDF convention). Page space: what a rendering at zoom=1 shows,
i.e. user space with the page's /Rotate applied around the crop box.

The crop offset is (cropbox.x0, cropbox.y0): user-space point p maps to
p' = p - crop_offset in the cropped, unrotated frame. Rotation then maps the
cropped frame into page space with dimensions (W, H) = the rotated crop size.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Point, Rect


@dataclass(frozen=True)
class PageTransform:
    """User space <-> page space for one page (crop offset + rotation)."""

    crop_x0: float
    crop_y0: float
    crop_w: float
    crop_h: float
    rotation: int  # 0, 90, 180, 270 — the page's effective /Rotate

    @property
    def page_width(self) -> float:
        return self.crop_h if self.rotation in (90, 270) else self.crop_w

    @property
    def page_height(self) -> float:
        return self.crop_w if self.rotation in (90, 270) else self.crop_h

    # -- user space -> cropped-unrotated frame -------------------------------
    def to_cropped(self, p: Point) -> Point:
        return Point(p.x - self.crop_x0, p.y - self.crop_y0)

    def rect_to_cropped(self, r: Rect) -> Rect:
        a = self.to_cropped(Point(r.x0, r.y0))
        b = self.to_cropped(Point(r.x1, r.y1))
        return Rect(a.x, a.y, b.x, b.y)

    # -- cropped frame -> page space (apply /Rotate) --------------------------
    def cropped_to_page(self, p: Point) -> Point:
        w, h = self.crop_w, self.crop_h
        rot = self.rotation % 360
        if rot == 0:
            return Point(p.x, p.y)
        if rot == 90:  # page displayed rotated 90° clockwise
            return Point(h - p.y, p.x)
        if rot == 180:
            return Point(w - p.x, h - p.y)
        if rot == 270:
            return Point(p.y, w - p.x)
        raise ValueError(f"unsupported rotation {self.rotation}")

    def page_to_cropped(self, p: Point) -> Point:
        w, h = self.crop_w, self.crop_h
        rot = self.rotation % 360
        if rot == 0:
            return Point(p.x, p.y)
        if rot == 90:
            return Point(p.y, h - p.x)
        if rot == 180:
            return Point(w - p.x, h - p.y)
        if rot == 270:
            return Point(w - p.y, p.x)
        raise ValueError(f"unsupported rotation {self.rotation}")

    def rect_to_page(self, r: Rect) -> Rect:
        corners = [
            self.cropped_to_page(Point(r.x0, r.y0)),
            self.cropped_to_page(Point(r.x1, r.y0)),
            self.cropped_to_page(Point(r.x0, r.y1)),
            self.cropped_to_page(Point(r.x1, r.y1)),
        ]
        xs = [c.x for c in corners]
        ys = [c.y for c in corners]
        return Rect(min(xs), min(ys), max(xs), max(ys))

    def rect_to_user(self, r_page: Rect) -> Rect:
        corners = [
            self.to_user(Point(r_page.x0, r_page.y0)),
            self.to_user(Point(r_page.x1, r_page.y0)),
            self.to_user(Point(r_page.x0, r_page.y1)),
            self.to_user(Point(r_page.x1, r_page.y1)),
        ]
        xs = [c.x for c in corners]
        ys = [c.y for c in corners]
        return Rect(min(xs), min(ys), max(xs), max(ys))

    def to_user(self, p_page: Point) -> Point:
        c = self.page_to_cropped(p_page)
        return Point(c.x + self.crop_x0, c.y + self.crop_y0)


@dataclass(frozen=True)
class ViewTransform:
    """Page space <-> view (widget logical px), then <-> device px."""

    zoom: float
    scroll_x: float
    scroll_y: float
    page_origin_x: float = 0.0  # where this page's top-left sits in the viewport
    page_origin_y: float = 0.0
    dpr: float = 1.0  # device pixel ratio

    def page_to_view(self, p: Point) -> Point:
        return Point(
            p.x * self.zoom + self.page_origin_x - self.scroll_x,
            p.y * self.zoom + self.page_origin_y - self.scroll_y,
        )

    def view_to_page(self, p: Point) -> Point:
        return Point(
            (p.x - self.page_origin_x + self.scroll_x) / self.zoom,
            (p.y - self.page_origin_y + self.scroll_y) / self.zoom,
        )

    def rect_page_to_view(self, r: Rect) -> Rect:
        a = self.page_to_view(Point(r.x0, r.y0))
        b = self.page_to_view(Point(r.x1, r.y1))
        return Rect(a.x, a.y, b.x, b.y)

    def rect_view_to_page(self, r: Rect) -> Rect:
        a = self.view_to_page(Point(r.x0, r.y0))
        b = self.view_to_page(Point(r.x1, r.y1))
        return Rect(a.x, a.y, b.x, b.y)

    def view_to_device(self, p: Point) -> Point:
        return Point(p.x * self.dpr, p.y * self.dpr)

    def device_to_view(self, p: Point) -> Point:
        return Point(p.x / self.dpr, p.y / self.dpr)


class CoordinateService:
    """Composes page + view transforms for a page shown in a viewport."""

    def __init__(self, page: PageTransform, view: ViewTransform):
        self.page = page
        self.view = view

    def user_to_view(self, p: Point) -> Point:
        return self.view.page_to_view(self.page.cropped_to_page(self.page.to_cropped(p)))

    def view_to_user(self, p: Point) -> Point:
        return self.page.to_user(self.view.view_to_page(p))

    def rect_user_to_view(self, r: Rect) -> Rect:
        return self.view.rect_page_to_view(self.page.rect_to_page(self.page.rect_to_cropped(r)))

    def rect_view_to_user(self, r: Rect) -> Rect:
        return self.page.rect_to_user(self.view.rect_view_to_page(r))
