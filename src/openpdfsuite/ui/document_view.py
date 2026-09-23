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

from ..domain.models import TextRegion
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

        # search highlights
        for rect in self.view.search_highlights.get(self.index, ()):  # page-space pts
            r = QRectF(pr.x() + rect[0] * self.zoom, pr.y() + rect[1] * self.zoom,
                       rect[2] * self.zoom, rect[3] * self.zoom)
            painter.fillRect(r, QColor(255, 200, 0, 90))

        # copy-selection highlight
        for rect in self.view.selection_rects.get(self.index, ()):
            r = QRectF(pr.x() + rect[0] * self.zoom, pr.y() + rect[1] * self.zoom,
                       rect[2] * self.zoom, rect[3] * self.zoom)
            sel_color = QColor(t.accent)
            sel_color.setAlpha(60)
            painter.fillRect(r, sel_color)
            painter.setPen(QColor(t.accent))
            painter.drawRect(r)
            painter.setPen(Qt.NoPen)

        # edit-mode hover outline (gray when the engine cannot edit it)
        hover = self.view.hover_region_rect
        if self.view.mode == "edit" and hover and hover[0] == self.index:
            r = QRectF(pr.x() + hover[1][0] * self.zoom, pr.y() + hover[1][1] * self.zoom,
                       hover[1][2] * self.zoom, hover[1][3] * self.zoom)
            pen = painter.pen()
            pen.setColor(QColor(t.accent) if len(hover) < 3 or hover[2]
                         else QColor("#98A2B3"))
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.drawRect(r)

        # selected-region outline (thicker, accent-filled tint)
        sel = self.view.selected_region_rect
        if self.view.mode == "edit" and sel and sel[0] == self.index:
            r = QRectF(pr.x() + sel[1][0] * self.zoom, pr.y() + sel[1][1] * self.zoom,
                       sel[1][2] * self.zoom, sel[1][3] * self.zoom)
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
            b = lay[1]
            r = QRectF(pr.x() + b[0] * self.zoom, pr.y() + b[1] * self.zoom,
                       (b[2] - b[0]) * self.zoom, (b[3] - b[1]) * self.zoom)
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
            for hx, hy in self.view.box_handles(b):
                px = pr.x() + hx * self.zoom
                py = pr.y() + hy * self.zoom
                painter.fillRect(QRectF(px - 4, py - 4, 8, 8), QColor(t.bg_panel))
                painter.drawRoundedRect(QRectF(px - 4, py - 4, 8, 8), 2, 2)
                painter.fillRect(QRectF(px - 3, py - 3, 6, 6), handle_brush)

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
        self.mode = "select"  # select | edit | add
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
        self._request_timer = QTimer(self)
        self._request_timer.setSingleShot(True)
        self._request_timer.setInterval(60)  # debounce zoom/scroll bursts
        self._request_timer.timeout.connect(self._request_visible)
        self.verticalScrollBar().valueChanged.connect(self._schedule_requests)
        self.horizontalScrollBar().valueChanged.connect(self._schedule_requests)
        theme.theme_changed.connect(lambda *_: self.viewport().update())

    # -- document lifecycle ------------------------------------------------
    def set_document(self, page_sizes: list[tuple[float, float]]) -> None:
        self.clear_document()
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
        self.page_lines.clear()
        self.page_regions.clear()
        self.search_highlights.clear()
        self.selection_rects.clear()
        self._pending.clear()
        self.current_page_changed.emit(0)

    def _dpr(self) -> float:
        screen = self.screen() or QApplication.primaryScreen()
        return screen.devicePixelRatio() if screen else 1.0

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

    # -- mode ---------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        """'select' | 'edit' | 'add'."""
        if mode == self.mode:
            return
        self.mode = mode
        self.hover_region_rect = None
        self.selected_region_rect = None
        self._pending_regions.clear()
        self._box_drag = None
        if mode == "add":
            self.layout_box_rect = None
        for frame in self.frames:
            frame.setCursor(Qt.IBeamCursor if mode == "select" else Qt.ArrowCursor)
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
        x, y = px.x(), px.y()
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
        x0, y0, x1, y1 = lay[1]
        return x0 <= px.x() <= x1 and y0 <= px.y() <= y1

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
                self._box_drag = {"op": "resize", "handle": handle,
                                  "start": frame.map_to_page(event.position().toPoint()),
                                  "orig": self.layout_box_rect[1],
                                  "page": frame.index}
                return
            if self._inside_layout_box(frame, event.position().toPoint()):
                self._box_drag = {"op": "move",
                                  "start": frame.map_to_page(event.position().toPoint()),
                                  "orig": self.layout_box_rect[1],
                                  "page": frame.index}
                return
            region = self._region_at(frame, event.position().toPoint())
            if region is not None:
                self.region_clicked.emit(frame.index, region)
        elif self.mode == "add":
            page_pos = frame.map_to_page(event.position().toPoint())
            if page_pos is not None:
                self.add_text_requested.emit(
                    frame.index, float(page_pos.x()), float(page_pos.y()))

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

    def _update_box_drag(self, frame: PageFrame, pos: QPoint) -> None:
        px = frame.map_to_page(pos)
        if px is None:
            return
        drag = self._box_drag
        x0, y0, x1, y1 = drag["orig"]
        dx = px.x() - drag["start"].x()
        dy = px.y() - drag["start"].y()
        page_w, page_h = frame.size_pt
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
        x, y = page_pos.x(), page_pos.y()
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
        sel = QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y1)).adjusted(
            -2, -2, 2, 2)
        rects: list[tuple] = []
        texts: list[str] = []
        for text, bbox, _baseline in self.page_lines.get(frame.index, []):
            r = QRectF(*bbox)
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
        super().keyPressEvent(event)
