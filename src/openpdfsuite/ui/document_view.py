"""Central document canvas (AGENTS.md §13).

Continuous vertical scroll of page frames. Rendering is virtualized: only
visible pages (plus a one-page buffer) are requested from the worker; results
land in an LRU cache keyed by (doc, revision, page, zoom, dpr). Stale or
superseded renders are dropped by the controller before they reach this widget.

Pages stay white in both themes; only the backdrop changes (AGENTS.md §4).
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..domain.coordinates import PageTransform
from ..domain.models import Point, Rect, TextRegion
from .themes import ThemeManager

PAGE_MARGIN = 14  # px around each page (4/8 scale: 14 ≈ 8+6 visual gutter)
PAGE_GAP = 10
BUFFER_PAGES = 1


class PageFrame(QWidget):
    """One rendered page. Draws pixmap or an honest placeholder state."""

    def __init__(self, index: int, size_pt: tuple[float, float], view: DocumentView):
        super().__init__(view.container)
        self.index = index
        self.size_pt = size_pt
        self.view = view
        self.zoom = 1.0
        self.pixmap: QPixmap | None = None
        self.state = "loading"  # loading | ready | failed
        self.setCursor(Qt.IBeamCursor)
        self.setMouseTracking(True)

    def set_zoom(self, zoom: float) -> None:
        self.zoom = zoom
        w = int(self.size_pt[0] * zoom) + 2 * PAGE_MARGIN
        h = int(self.size_pt[1] * zoom) + 2 * PAGE_MARGIN
        self.setFixedSize(w, h)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self.pixmap = pixmap
        self.state = "ready"
        self.update()

    def clear_pixmap(self) -> None:
        self.pixmap = None
        self.state = "loading"
        self.update()

    def page_rect(self) -> QRectF:
        return QRectF(PAGE_MARGIN, PAGE_MARGIN,
                      self.size_pt[0] * self.zoom, self.size_pt[1] * self.zoom)

    def map_to_page(self, pos: QPoint) -> QPoint | None:
        pr = self.page_rect()
        if not pr.contains(pos.x(), pos.y()):
            return None
        return QPoint(int((pos.x() - pr.x()) / self.zoom),
                      int((pos.y() - pr.y()) / self.zoom))

    # -- painting ----------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        t = self.view.theme.tokens
        pr = self.page_rect()

        # subtle shadow + white page (white in BOTH themes)
        shadow = QColor(*t.shadow_rgba)
        painter.fillRect(QRectF(pr).adjusted(2, 2, 2, 2), shadow)
        painter.fillRect(pr, QColor("#FFFFFF"))
        painter.setPen(QColor(t.border))
        painter.drawRect(pr)

        if self.pixmap is not None and not self.pixmap.isNull():
            painter.drawPixmap(int(pr.x()), int(pr.y()),
                               int(pr.width()), int(pr.height()), self.pixmap)
        else:
            painter.setPen(QColor("#98A2B3"))
            painter.drawText(pr, Qt.AlignCenter,
                             "Loading…" if self.state == "loading" else "Page failed to render")

        # search highlights (engine space -> display space)
        for rect in self.view.search_highlights.get(self.index, ()):  # engine pts
            r = self.view.rect_to_display(self.index, Rect(*rect))
            r = QRectF(pr.x() + r.x0 * self.zoom, pr.y() + r.y0 * self.zoom,
                       r.width * self.zoom, r.height * self.zoom)
            painter.fillRect(r, QColor(255, 200, 0, 90))

        # copy-selection highlight (engine space -> display space)
        for rect in self.view.selection_rects.get(self.index, ()):
            er = self.view.rect_to_display(self.index, Rect(*rect))
            r = QRectF(pr.x() + er.x0 * self.zoom, pr.y() + er.y0 * self.zoom,
                       er.width * self.zoom, er.height * self.zoom)
            sel_color = QColor(t.accent)
            sel_color.setAlpha(60)
            painter.fillRect(r, sel_color)
            painter.setPen(QColor(t.accent))
            painter.drawRect(r)
            painter.setPen(Qt.NoPen)

        # edit-mode hover outline (gray when the engine cannot edit it)
        hover = self.view.hover_region_rect
        if self.view.mode == "edit" and hover and hover[0] == self.index:
            er = self.view.rect_to_display(self.index, Rect(*hover[1]))
            r = QRectF(pr.x() + er.x0 * self.zoom, pr.y() + er.y0 * self.zoom,
                       er.width * self.zoom, er.height * self.zoom)
            pen = painter.pen()
            pen.setColor(QColor(t.accent) if len(hover) < 3 or hover[2]
                         else QColor("#98A2B3"))
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.drawRect(r)

        # selected-region outline (thicker, accent-filled tint)
        sel = self.view.selected_region_rect
        if self.view.mode == "edit" and sel and sel[0] == self.index:
            er = self.view.rect_to_display(self.index, Rect(*sel[1]))
            r = QRectF(pr.x() + er.x0 * self.zoom, pr.y() + er.y0 * self.zoom,
                       er.width * self.zoom, er.height * self.zoom)
            tint = QColor(t.accent)
            tint.setAlpha(36)
            painter.fillRect(r, tint)
            pen = painter.pen()
            pen.setColor(QColor(t.accent))
            pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.drawRect(r)
            painter.setPen(Qt.NoPen)

        # layout box (Mode B target): dashed outline + resize handles
        lay = self.view.layout_box_rect
        if self.view.mode in ("edit", "add") and lay and lay[0] == self.index:
            er = self.view.rect_to_display(self.index, Rect(*lay[1]))
            r = QRectF(pr.x() + er.x0 * self.zoom, pr.y() + er.y0 * self.zoom,
                       er.width * self.zoom, er.height * self.zoom)
            pen = painter.pen()
            pen.setColor(QColor(t.accent))
            pen.setStyle(Qt.DashLine)
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(r)
            pen.setStyle(Qt.SolidLine)
            painter.setPen(pen)
            handle_brush = QColor(t.accent)
            for hx, hy in self.view.box_handles((er.x0, er.y0, er.x1, er.y1)):
                px = pr.x() + hx * self.zoom
                py = pr.y() + hy * self.zoom
                painter.fillRect(QRectF(px - 4, py - 4, 8, 8), QColor(t.bg_panel))
                painter.drawRoundedRect(QRectF(px - 4, py - 4, 8, 8), 2, 2)
                painter.fillRect(QRectF(px - 3, py - 3, 6, 6), handle_brush)

        # black-out marks (M9): red tint + border, distinct from search yellow
        # and the accent blue; also the dashed in-progress marquee
        red = QColor("#DC2626")
        for rect in self.view.redact_marks.get(self.index, ()):
            er = self.view.rect_to_display(self.index, rect)
            r = QRectF(pr.x() + er.x0 * self.zoom, pr.y() + er.y0 * self.zoom,
                       er.width * self.zoom, er.height * self.zoom)
            tint = QColor(red)
            tint.setAlpha(46)
            painter.fillRect(r, tint)
            pen = painter.pen()
            pen.setColor(red)
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(r)
        if self.view.mode == "redact" and self.view._redact_drag_rect is not None \
                and self.view._redact_drag is not None \
                and self.view._redact_drag[0] == self.index:
            d = self.view._redact_drag_rect
            r = QRectF(pr.x() + d[0] * self.zoom, pr.y() + d[1] * self.zoom,
                       (d[2] - d[0]) * self.zoom, (d[3] - d[1]) * self.zoom)
            pen = painter.pen()
            pen.setColor(red)
            pen.setStyle(Qt.DashLine)
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(r)
            pen.setStyle(Qt.SolidLine)

    # -- mouse -----------------------------------------------------------
    def mousePressEvent(self, event):
        self.view._frame_mouse_press(self, event)

    def mouseMoveEvent(self, event):
        self.view._frame_mouse_move(self, event)

    def mouseReleaseEvent(self, event):
        self.view._frame_mouse_release(self, event)

    def mouseDoubleClickEvent(self, event):
        self.view._frame_mouse_double(self, event)

    def leaveEvent(self, event):
        self.view._frame_leave(self)


class DocumentView(QScrollArea):
    """Virtualized multipage canvas."""

    current_page_changed = Signal(int)  # 0-based
    zoom_changed = Signal(float)
    status_message = Signal(str)
    selection_changed = Signal(str)  # selected text (copy support)
    # M3 hooks
    region_hovered = Signal(int, object)  # page, TextRegion|None
    region_clicked = Signal(int, object)
    region_double_clicked = Signal(int, object)

    def __init__(self, theme: ThemeManager, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.container = QWidget()
        self.container.setObjectName("CanvasContainer")
        # QSS-painted canvas (see tokens.py): plain QWidgets need this
        # attribute to paint their stylesheet background at all.
        self.container.setAttribute(Qt.WA_StyledBackground, True)
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(0, PAGE_GAP, 0, PAGE_GAP)
        self.layout.setSpacing(PAGE_GAP)
        self.layout.setAlignment(Qt.AlignHCenter)
        self.setWidget(self.container)
        self.setWidgetResizable(True)
        self.viewport().setObjectName("CanvasViewport")
        self.viewport().setAttribute(Qt.WA_StyledBackground, True)
        self.frames: list[PageFrame] = []
        self.mode = "select"  # select | edit | add | redact
        self.page_rotations: list[int] = []  # per-page /Rotate from the worker
        self.search_highlights: dict[int, list[tuple]] = {}
        self.selection_rects: dict[int, list[tuple]] = {}
        self.selected_text = ""
        self.hover_region_rect: tuple[int, tuple] | None = None
        self.selected_region_rect: tuple[int, tuple] | None = None
        self.layout_box_rect: tuple[int, tuple] | None = None
        self._box_drag: dict | None = None
        self.page_lines: dict[int, list[tuple[str, tuple, float]]] = {}
        self.page_regions: dict[int, list[TextRegion]] = {}
        self._pending: set[tuple[int, float]] = set()
        self._pending_regions: set[int] = set()
        self._drag_start: tuple[int, QPoint] | None = None
        # M9 redaction marks: engine-space rects per page; the black-out tool
        self.redact_marks: dict[int, list[Rect]] = {}
        self._redact_drag: tuple[int, QPoint] | None = None
        self._redact_drag_rect: tuple | None = None  # display-space, for painting
        self._request_timer = QTimer(self)
        self._request_timer.setSingleShot(True)
        self._request_timer.setInterval(60)  # debounce zoom/scroll bursts
        self._request_timer.timeout.connect(self._request_visible)
        self.verticalScrollBar().valueChanged.connect(self._schedule_requests)
        self.horizontalScrollBar().valueChanged.connect(self._schedule_requests)
        theme.theme_changed.connect(lambda *_: self.viewport().update())

    # -- document lifecycle ------------------------------------------------
    def set_document(self, page_sizes: list[tuple[float, float]],
                     rotations: list[int] | None = None) -> None:
        self.clear_document()
        self.page_rotations = list(rotations or [])
        for i, size in enumerate(page_sizes):
            frame = PageFrame(i, size, self)
            self.frames.append(frame)
            self.layout.addWidget(frame)
        self._pending_fit = True
        self._schedule_requests()

    def _apply_pending_fit(self) -> None:
        """Fit width once the viewport has a real size (first show/resize)."""
        if getattr(self, "_pending_fit", False) and self.viewport().width() > 100:
            self._pending_fit = False
            self.fit_width()

    def clear_document(self) -> None:
        for frame in self.frames:
            self.layout.removeWidget(frame)
            frame.deleteLater()
        self.frames.clear()
        self.page_rotations = []
        self.page_lines.clear()
        self.page_regions.clear()
        self.search_highlights.clear()
        self.selection_rects.clear()
        self.redact_marks.clear()
        self._redact_drag = None
        self._redact_drag_rect = None
        self._pending.clear()
        self.current_page_changed.emit(0)

    def _dpr(self) -> float:
        screen = self.screen() or QApplication.primaryScreen()
        return screen.devicePixelRatio() if screen else 1.0

    # -- engine <-> display space (AGENTS.md §8) --------------------------------
    # Engine geometry (regions, lines, layout boxes — everything the worker
    # sends) lives in PyMuPDF's unrotated, crop-normal page space. The canvas
    # shows the rotated rendering, so `map_to_page` (mouse on the pixmap)
    # yields DISPLAY coordinates. Every crossing between the two goes through
    # PageTransform here; at rotation 0 both spaces are identical.
    def _page_tf(self, index: int) -> PageTransform:
        if not 0 <= index < len(self.frames):
            raise IndexError(index)
        w, h = self.frames[index].size_pt  # display dims
        rot = self.page_rotations[index] if index < len(self.page_rotations) else 0
        uw, uh = (h, w) if rot in (90, 270) else (w, h)  # unrotated dims
        return PageTransform(crop_x0=0.0, crop_y0=0.0, crop_w=uw, crop_h=uh,
                             rotation=rot)

    def rect_to_display(self, page: int, rect: Rect) -> Rect:
        """Engine-space rect -> display-space rect (for painting)."""
        return self._page_tf(page).rect_to_page(rect)

    def rect_to_engine(self, page: int, rect: Rect) -> Rect:
        """Display-space rect -> engine-space rect (hit tests)."""
        return self._page_tf(page).rect_to_user(rect)

    def point_to_engine(self, page: int, x: float, y: float) -> Point:
        """Display-space point -> engine-space point (mouse)."""
        return self._page_tf(page).to_user(Point(x, y))

    def page_size_engine(self, page: int) -> tuple[float, float]:
        """(width, height) of the page in engine (unrotated) space."""
        tf = self._page_tf(page)
        return tf.crop_w, tf.crop_h

    # -- zoom -----------------------------------------------------------------
    @property
    def zoom(self) -> float:
        return self.frames[0].zoom if self.frames else 1.0

    def set_zoom(self, zoom: float, keep_page: bool = True) -> None:
        if not self.frames:
            return
        zoom = max(0.1, min(8.0, zoom))
        current = self.current_page()
        for frame in self.frames:
            frame.set_zoom(zoom)
            frame.clear_pixmap()
        self._pending.clear()
        self.container.adjustSize()
        self.zoom_changed.emit(zoom)
        if keep_page:
            self.scroll_to_page(current, instant=True)
        self._schedule_requests()

    def fit_width(self) -> None:
        if not self.frames:
            return
        avail = self.viewport().width() - 2 * PAGE_MARGIN - 24
        widest = max(f.size_pt[0] for f in self.frames)
        self.set_zoom(avail / widest)

    def fit_page(self) -> None:
        if not self.frames:
            return
        avail_w = self.viewport().width() - 2 * PAGE_MARGIN - 24
        avail_h = self.viewport().height() - 2 * PAGE_MARGIN - 24
        f0 = self.frames[0]
        self.set_zoom(min(avail_w / f0.size_pt[0], avail_h / f0.size_pt[1]))

    # -- navigation ----------------------------------------------------------
    def current_page(self) -> int:
        if not self.frames:
            return 0
        center_y = self.verticalScrollBar().value() + self.viewport().height() // 2
        for frame in self.frames:
            top = frame.y()
            if top <= center_y < top + frame.height():
                return frame.index
        return self.frames[-1].index

    def scroll_to_page(self, index: int, instant: bool = False) -> None:
        if 0 <= index < len(self.frames):
            frame = self.frames[index]
            y = frame.y() - PAGE_GAP
            if instant:
                self.verticalScrollBar().setValue(y)
            else:
                self.verticalScrollBar().setValue(y)
            self.current_page_changed.emit(index)
            self._schedule_requests()

    # -- render requests ------------------------------------------------------
    def _schedule_requests(self) -> None:
        self._request_timer.start()

    def _visible_range(self) -> tuple[int, int]:
        if not self.frames:
            return (0, -1)
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        first = last = None
        for frame in self.frames:
            if frame.y() + frame.height() >= top and frame.y() <= bottom:
                if first is None:
                    first = frame.index
                last = frame.index
        if first is None:
            # between pages: find nearest above
            for frame in self.frames:
                if frame.y() <= top:
                    first = last = frame.index
            if first is None:
                first = last = 0
        return (max(0, first - BUFFER_PAGES), min(len(self.frames) - 1, last + BUFFER_PAGES))

    def _request_visible(self) -> None:
        if not self.frames:
            return
        first, last = self._visible_range()
        zoom = self.zoom
        dpr = self._dpr()
        current = self.current_page()
        self.current_page_changed.emit(current)
        for frame in self.frames[first:last + 1]:
            key = (frame.index, zoom)
            if key in self._pending or frame.pixmap is not None:
                continue
            self._pending.add(key)
            self.view_request_page.emit(frame.index, zoom, dpr)
        # text lines for visible pages feed selection + editing hit tests
        for frame in self.frames[first:last + 1]:
            if frame.index not in self.page_lines:
                self.view_request_lines.emit(frame.index)
            if self.mode == "edit" and frame.index not in self.page_regions:
                if frame.index not in self._pending_regions:
                    self._pending_regions.add(frame.index)
                    self.view_request_regions.emit(frame.index)

    view_request_page = Signal(int, float, float)  # page, zoom, dpr
    view_request_lines = Signal(int)  # page
    view_request_regions = Signal(int)  # page (edit mode)
    region_box_changed = Signal(int, tuple)  # page, box (layout box moved/resized)
    add_text_requested = Signal(int, float, float)  # page, x_pt, y_pt
    redact_marks_changed = Signal()  # M9: any mark added/removed/cleared

    # -- mode ---------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        """'select' | 'edit' | 'add' | 'redact'."""
        if mode == self.mode:
            return
        self.mode = mode
        self.hover_region_rect = None
        self.selected_region_rect = None
        self._pending_regions.clear()
        self._box_drag = None
        self._redact_drag = None
        self._redact_drag_rect = None
        if mode in ("add", "redact"):
            self.layout_box_rect = None
        for frame in self.frames:
            if mode == "select":
                frame.setCursor(Qt.IBeamCursor)
            elif mode == "redact":
                frame.setCursor(Qt.CrossCursor)
            else:
                frame.setCursor(Qt.ArrowCursor)
            frame.update()
        if mode == "edit":
            self._schedule_requests()

    def set_selected_region(self, page: int | None, bbox: tuple | None) -> None:
        self.selected_region_rect = (page, bbox) if page is not None and bbox else None
        for frame in self.frames:
            frame.update()

    def set_layout_box(self, page: int | None, box: tuple | None) -> None:
        self.layout_box_rect = (page, box) if page is not None and box else None
        for frame in self.frames:
            frame.update()

    # -- redaction marks (M9) --------------------------------------------------
    def get_redact_marks(self) -> list[tuple[int, Rect]]:
        """All pending black-out marks as (page, engine-space Rect)."""
        return [(page, r) for page, rects in self.redact_marks.items() for r in rects]

    def add_redact_mark(self, page: int, rect: Rect) -> None:
        self.redact_marks.setdefault(page, []).append(rect)
        self._repaint_page(page)
        self.redact_marks_changed.emit()

    def remove_redact_mark(self, page: int, rect: Rect) -> None:
        marks = self.redact_marks.get(page, [])
        if rect in marks:
            marks.remove(rect)
            if not marks:
                self.redact_marks.pop(page, None)
            self._repaint_page(page)
            self.redact_marks_changed.emit()

    def clear_redact_marks(self) -> None:
        pages = list(self.redact_marks)
        self.redact_marks.clear()
        self._redact_drag = None
        self._redact_drag_rect = None
        for page in pages:
            self._repaint_page(page)
        if pages:
            self.redact_marks_changed.emit()

    def has_redact_marks(self) -> bool:
        return any(self.redact_marks.values())

    def _repaint_page(self, page: int) -> None:
        if 0 <= page < len(self.frames):
            self.frames[page].update()

    # -- layout-box geometry ------------------------------------------------------
    _HANDLES = ("tl", "tr", "bl", "br", "t", "b", "l", "r")

    def box_handles(self, box: tuple) -> list[tuple[float, float]]:
        """Handle anchor points in page pts for the current layout box."""
        x0, y0, x1, y1 = box
        return [
            (x0, y0), (x1, y0), (x0, y1), (x1, y1),
            ((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1),
            (x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2),
        ]

    def _handle_at(self, frame: PageFrame, pos: QPoint) -> str | None:
        lay = self.layout_box_rect
        if not lay or lay[0] != frame.index:
            return None
        tol = 7.0 / frame.zoom  # 7 view px
        px = frame.map_to_page(pos)
        if px is None:
            return None
        pt = self.point_to_engine(frame.index, px.x(), px.y())
        x, y = pt.x, pt.y
        for name, (hx, hy) in zip(self._HANDLES, self.box_handles(lay[1]), strict=True):
            if abs(x - hx) <= tol and abs(y - hy) <= tol:
                return name
        return None

    def _inside_layout_box(self, frame: PageFrame, pos: QPoint) -> bool:
        lay = self.layout_box_rect
        if not lay or lay[0] != frame.index:
            return False
        px = frame.map_to_page(pos)
        if px is None:
            return False
        pt = self.point_to_engine(frame.index, px.x(), px.y())
        x0, y0, x1, y1 = lay[1]
        return x0 <= pt.x <= x1 and y0 <= pt.y <= y1

    _HANDLE_CURSORS = {
        "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
        "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
        "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
        "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
    }

    # -- result slots (controller connects here) --------------------------------
    def on_page_rendered(self, page: int, pixmap: QPixmap, zoom: float,
                         request_id: int) -> None:
        self._pending.discard((page, zoom))
        if not (0 <= page < len(self.frames)):
            return
        frame = self.frames[page]
        if abs(frame.zoom - zoom) > 1e-6:
            return  # stale zoom level
        frame.set_pixmap(pixmap)

    def on_render_failed(self, page: int, zoom: float) -> None:
        self._pending.discard((page, zoom))
        if 0 <= page < len(self.frames):
            self.frames[page].state = "failed"
            self.frames[page].update()

    def on_page_lines(self, page: int, lines: list, revision: int) -> None:
        self.page_lines[page] = lines

    def on_regions(self, page: int, regions: list, revision: int) -> None:
        self.page_regions[page] = regions
        self._pending_regions.discard(page)

    def refresh_after_edit(self, page: int) -> None:
        """Revision changed: drop cached pixmaps for this page and re-request."""
        self.page_lines.pop(page, None)
        self.page_regions.pop(page, None)
        self._pending_regions.discard(page)
        self.hover_region_rect = None
        self.selected_region_rect = None
        if 0 <= page < len(self.frames):
            self.frames[page].clear_pixmap()
        self._schedule_requests()

    # -- search highlights -------------------------------------------------------
    def set_search_highlights(self, highlights: dict[int, list[tuple]]) -> None:
        self.search_highlights = highlights
        for page in highlights:
            if 0 <= page < len(self.frames):
                self.frames[page].update()

    def clear_search_highlights(self) -> None:
        pages = list(self.search_highlights)
        self.search_highlights.clear()
        for page in pages:
            if 0 <= page < len(self.frames):
                self.frames[page].update()

    # -- copy selection -----------------------------------------------------------
    def _frame_mouse_press(self, frame: PageFrame, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        if self.mode == "select":
            page_pos = frame.map_to_page(event.position().toPoint())
            if page_pos is not None:
                self._drag_start = (frame.index, page_pos)
                self.selection_rects.clear()
                self.selected_text = ""
                self.selection_changed.emit("")
        elif self.mode == "edit":
            handle = self._handle_at(frame, event.position().toPoint())
            if handle is not None and self.layout_box_rect:
                mpp = frame.map_to_page(event.position().toPoint())
                self._box_drag = {"op": "resize", "handle": handle,
                                  "start": self.point_to_engine(frame.index, mpp.x(), mpp.y()),
                                  "orig": self.layout_box_rect[1],
                                  "page": frame.index}
                return
            if self._inside_layout_box(frame, event.position().toPoint()):
                mpp = frame.map_to_page(event.position().toPoint())
                self._box_drag = {"op": "move",
                                  "start": self.point_to_engine(frame.index, mpp.x(), mpp.y()),
                                  "orig": self.layout_box_rect[1],
                                  "page": frame.index}
                return
            region = self._region_at(frame, event.position().toPoint())
            if region is not None:
                self.region_clicked.emit(frame.index, region)
        elif self.mode == "add":
            page_pos = frame.map_to_page(event.position().toPoint())
            if page_pos is not None:
                pt = self.point_to_engine(frame.index, page_pos.x(), page_pos.y())
                self.add_text_requested.emit(frame.index, float(pt.x), float(pt.y))
        elif self.mode == "redact":
            page_pos = frame.map_to_page(event.position().toPoint())
            if page_pos is None:
                return
            pt = self.point_to_engine(frame.index, page_pos.x(), page_pos.y())
            # click inside an existing mark removes it; otherwise start a marquee
            for rect in self.redact_marks.get(frame.index, []):
                if rect.x0 - 2 <= pt.x <= rect.x1 + 2 and rect.y0 - 2 <= pt.y <= rect.y1 + 2:
                    self.remove_redact_mark(frame.index, rect)
                    self.status_message.emit(
                        "Mark removed. All marks stay until you apply them.")
                    return
            self._redact_drag = (frame.index, page_pos)
            self._redact_drag_rect = (page_pos.x(), page_pos.y(),
                                      page_pos.x(), page_pos.y())
            frame.update()

    def _frame_mouse_move(self, frame: PageFrame, event) -> None:
        if self.mode == "select" and self._drag_start is not None:
            page_pos = frame.map_to_page(event.position().toPoint())
            if page_pos is not None and frame.index == self._drag_start[0]:
                self._update_selection(frame, page_pos)
        elif self.mode == "edit":
            if self._box_drag is not None and self._box_drag["page"] == frame.index:
                self._update_box_drag(frame, event.position().toPoint())
                return
            handle = self._handle_at(frame, event.position().toPoint())
            if handle is not None:
                frame.setCursor(self._HANDLE_CURSORS[handle])
            elif self._inside_layout_box(frame, event.position().toPoint()):
                frame.setCursor(Qt.SizeAllCursor)
            else:
                frame.setCursor(Qt.ArrowCursor)
            region = self._region_at(frame, event.position().toPoint())
            self.hover_region_rect = (
                (frame.index, region.bbox.as_tuple(),
                 bool(getattr(region, "editable", True))) if region else None
            )
            frame.update()
            self.region_hovered.emit(frame.index, region)
        elif self.mode == "redact" and self._redact_drag is not None:
            page_pos = frame.map_to_page(event.position().toPoint())
            if page_pos is not None and frame.index == self._redact_drag[0]:
                sx, sy = self._redact_drag[1].x(), self._redact_drag[1].y()
                self._redact_drag_rect = (min(sx, page_pos.x()), min(sy, page_pos.y()),
                                          max(sx, page_pos.x()), max(sy, page_pos.y()))
                frame.update()

    def _update_box_drag(self, frame: PageFrame, pos: QPoint) -> None:
        px = frame.map_to_page(pos)
        if px is None:
            return
        drag = self._box_drag
        cur = self.point_to_engine(frame.index, px.x(), px.y())
        x0, y0, x1, y1 = drag["orig"]
        dx = cur.x - drag["start"].x
        dy = cur.y - drag["start"].y
        tf = self._page_tf(frame.index)
        page_w, page_h = tf.crop_w, tf.crop_h  # engine-space page dims
        min_size = 12.0
        if drag["op"] == "move":
            w, h = x1 - x0, y1 - y0
            nx0 = max(0.0, min(x0 + dx, page_w - w))
            ny0 = max(0.0, min(y0 + dy, page_h - h))
            box = (nx0, ny0, nx0 + w, ny0 + h)
        else:
            h_name = drag["handle"]
            if "l" in h_name:
                x0 = max(0.0, min(x0 + dx, x1 - min_size))
            if "r" in h_name:
                x1 = min(page_w, max(x1 + dx, x0 + min_size))
            if "t" in h_name:
                y0 = max(0.0, min(y0 + dy, y1 - min_size))
            if "b" in h_name:
                y1 = min(page_h, max(y1 + dy, y0 + min_size))
            box = (x0, y0, x1, y1)
        self.layout_box_rect = (drag["page"], box)
        frame.update()

    def _frame_mouse_release(self, frame: PageFrame, event) -> None:
        if self._box_drag is not None and self._box_drag["page"] == frame.index:
            orig = self._box_drag["orig"]
            self._box_drag = None
            lay = self.layout_box_rect
            if lay and any(abs(a - b) > 0.5 for a, b in zip(lay[1], orig, strict=True)):
                self.region_box_changed.emit(lay[0], lay[1])
            return
        if self._drag_start is not None:
            self._drag_start = None
            if self.selected_text:
                self.status_message.emit(
                    f"{len(self.selected_text.split())} words selected — Ctrl+C to copy")
            return
        if self.mode == "redact" and self._redact_drag is not None:
            drag_page, start = self._redact_drag
            self._redact_drag = None
            rect_disp = self._redact_drag_rect
            self._redact_drag_rect = None
            frame.update()
            if frame.index != drag_page or rect_disp is None:
                return
            w, h = rect_disp[2] - rect_disp[0], rect_disp[3] - rect_disp[1]
            if w < 3 or h < 3:  # click without drag on empty spot: no-op
                return
            disp = Rect(*rect_disp)
            mark = self.rect_to_engine(frame.index, disp)
            page_rect = self._engine_page_rect(frame)
            mark = Rect(max(mark.x0, page_rect.x0), max(mark.y0, page_rect.y0),
                        min(mark.x1, page_rect.x1), min(mark.y1, page_rect.y1))
            if mark.width < 1 or mark.height < 1:
                return
            self.add_redact_mark(frame.index, mark)
            n = len(self.get_redact_marks())
            self.status_message.emit(
                f"{n} area{'s' if n != 1 else ''} marked — Apply Redaction to "
                "remove the text permanently.")

    def _engine_page_rect(self, frame: PageFrame) -> Rect:
        tf = self._page_tf(frame.index)
        return Rect(0.0, 0.0, tf.crop_w, tf.crop_h)

    def _frame_mouse_double(self, frame: PageFrame, event) -> None:
        if self.mode == "edit":
            region = self._region_at(frame, event.position().toPoint())
            if region is not None:
                self.region_double_clicked.emit(frame.index, region)

    def _frame_leave(self, frame: PageFrame) -> None:
        if self.hover_region_rect and self.hover_region_rect[0] == frame.index:
            self.hover_region_rect = None
            frame.update()

    def _region_at(self, frame: PageFrame, pos: QPoint) -> TextRegion | None:
        page_pos = frame.map_to_page(pos)
        if page_pos is None:
            return None
        regions = self.page_regions.get(frame.index, [])
        # region bboxes are engine space; the mouse is on the rendered
        # (display) pixmap — compare in engine space
        pt = self.point_to_engine(frame.index, page_pos.x(), page_pos.y())
        x, y = pt.x, pt.y
        # prefer the tightest containing line region; paragraph regions are the fallback
        best_line = None
        best_para = None
        for region in regions:
            b = region.bbox
            if b.x0 - 2 <= x <= b.x1 + 2 and b.y0 - 2 <= y <= b.y1 + 2:
                if region.mode.name == "PRESERVE_LINE":
                    if best_line is None or region.bbox.height < best_line.bbox.height:
                        best_line = region
                elif best_para is None:
                    best_para = region
        return best_line or best_para

    def _update_selection(self, frame: PageFrame, page_pos: QPoint) -> None:
        start_page, start = self._drag_start
        if start_page != frame.index:
            return
        x0, y0 = start.x(), start.y()
        x1, y1 = page_pos.x(), page_pos.y()
        slop = 2.0 / frame.zoom  # 2 screen px, constant on screen
        disp = Rect(min(x0, x1) - slop, min(y0, y1) - slop,
                    max(x0, x1) + slop, max(y0, y1) + slop)
        sel = self.rect_to_engine(frame.index, disp)
        rects: list[tuple] = []
        texts: list[str] = []
        for text, bbox, _baseline in self.page_lines.get(frame.index, []):
            r = Rect(*bbox)
            if r.intersects(sel):
                rects.append(bbox)
                texts.append(text)
        self.selection_rects = {frame.index: rects} if rects else {}
        self.selected_text = "\n".join(texts)
        frame.update()
        self.selection_changed.emit(self.selected_text)

    def copy_selection(self) -> str:
        if self.selected_text:
            QApplication.clipboard().setText(self.selected_text)
        return self.selected_text

    # -- misc ----------------------------------------------------------------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_pending_fit()
        self._schedule_requests()

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            event.accept()
            return
        if event.key() == Qt.Key_Escape and self.mode == "redact" \
                and self.has_redact_marks():
            self.clear_redact_marks()
            self.status_message.emit("Black-out marks cleared.")
            event.accept()
            return
        super().keyPressEvent(event)
