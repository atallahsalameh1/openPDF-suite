"""Main application window (AGENTS.md §4).

Layout: compact menu bar, main toolbar, collapsible left sidebar (thumbnails +
search), large central canvas (welcome screen until a document is open),
context-sensitive right properties panel, status bar with page/zoom/messages.

Document work never happens here: this class routes user actions to the
DocumentController (application layer) and renders the signals it emits back.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QEventLoop, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QDialog,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..application.document_controller import DocumentController
from ..domain.models import Color, DocxExportResult, EditMode, Rect, ReplacementEdit, TextAlignment
from ..infrastructure.logging import get_logger
from ..infrastructure.recovery import RecoveryStore
from ..infrastructure.settings import Settings
from .dialogs.preview_dialog import OverflowDialog, PreviewDialog
from .document_view import DocumentView
from .panels import PropertiesPanel, SidebarTabs
from .text_overlay import TextOverlay
from .themes import ThemeManager
from .welcome_screen import WelcomeScreen

log = get_logger("main_window")

_PDF_MAGIC = b"%PDF-"

_ALIGN_FROM_STR = {
    "left": TextAlignment.LEFT,
    "center": TextAlignment.CENTER,
    "right": TextAlignment.RIGHT,
    "justify": TextAlignment.JUSTIFY,
}


def _looks_like_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
        return _PDF_MAGIC in head
    except OSError:
        return False


class LoadingWidget(QWidget):
    """Honest loading state while the worker opens the document."""

    def __init__(self, theme: ThemeManager, path: Path, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        icon = QLabel()
        icon.setPixmap(theme.icon("document", 48, theme.tokens.text_secondary).pixmap(48, 48))
        icon.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon)
        label = QLabel(f"Opening “{path.name}”…")
        label.setProperty("role", "secondary")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        layout.addStretch(2)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, theme: ThemeManager,
                 open_path: str | None = None):
        super().__init__()
        self.settings = settings
        self.theme = theme
        self._current_path: Path | None = None
        self.controller: DocumentController | None = None
        self.view: DocumentView | None = None
        # editing state (M3)
        self._selected_region: tuple[int, object] | None = None
        self._overlay: TextOverlay | None = None
        self._style_overrides: dict = {}
        self._pending_edit: dict | None = None
        self._saved_once = False
        self._saving = False
        self._converting_docx = False
        self._layout_box: tuple[int, tuple, str] | None = None
        self._insert_ctx: dict | None = None
        self._redact_queue: list[tuple[int, list]] = []
        self._redact_done: list[int] = []
        self._pending_redact: dict | None = None
        self._last_save_dest: str | None = None
        self._recovery_key: str | None = None  # entry key; None -> session doc_id
        self.recovery = RecoveryStore()
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setSingleShot(True)
        self._recovery_timer.setInterval(1500)  # debounce recovery writes (§11)
        self._recovery_timer.timeout.connect(self._write_recovery)
        QTimer.singleShot(0, self._check_recovery)

        self.setWindowTitle("openPDF suite")
        self.setMinimumSize(1100, 720)
        self.setAcceptDrops(True)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_central()
        self._build_docks()
        self._build_statusbar()
        self._update_action_availability()

        geo = settings.restore_geometry("main")
        if geo is not None:
            self.restoreGeometry(geo)
        state = settings.restore_state("main")
        if state is not None:
            self.restoreState(state)

        theme.theme_changed.connect(self._retheme)
        self._retheme()

        if open_path:
            self.open_document(open_path)

    # -- actions ----------------------------------------------------------------
    def _build_actions(self) -> None:
        def act(text, slot=None, shortcut=None, tip=None, enabled=True, checkable=False):
            a = QAction(text, self)
            ks = QKeySequence(shortcut) if shortcut is not None else None
            if ks is not None:
                a.setShortcut(ks)
            if tip:
                suffix = f" ({ks.toString(QKeySequence.NativeText)})" if ks else ""
                a.setToolTip(tip + suffix)
                a.setStatusTip(tip)
            a.setEnabled(enabled)
            a.setCheckable(checkable)
            if slot:
                a.triggered.connect(slot)
            return a

        self.action_open = act("&Open…", self._on_open, QKeySequence.Open, "Open a PDF")
        self.action_save = act("&Save", self._on_save, QKeySequence.Save,
                               "Save changes to the current file")
        self.action_save_as = act("Save &As…", self._on_save_as, QKeySequence("Ctrl+Shift+S"),
                                  "Save changes to a new file")
        self.action_convert_to_word = act(
            "Convert to &Word…", self._on_convert_to_word, QKeySequence("Ctrl+Shift+W"),
            "Convert the current PDF into a .docx document")
        self.action_close_doc = act("&Close Document", self._on_close_doc,
                                    QKeySequence("Ctrl+W"), "Close the current document")
        self.action_exit = act("E&xit", self.close, QKeySequence("Alt+F4"), "Quit openPDF suite")

        self.action_undo = act("&Undo", self._on_undo, QKeySequence.Undo,
                               "Undo the last edit")
        self.action_redo = act("&Redo", self._on_redo, QKeySequence.Redo,
                               "Redo the last undone edit")

        self.action_tool_select = act("&Select", self._set_tool_select, QKeySequence("V"),
                                      "Select and copy text (read mode)", checkable=True)
        self.action_tool_edit = act("&Edit Text", self._set_tool_edit, QKeySequence("E"),
                                    "Select and edit existing text", checkable=True)
        self.action_tool_add = act("Add &Text", self._set_tool_add, QKeySequence("T"),
                                   "Add new text to a page", checkable=True)
        self.action_tool_redact = act("&Black Out Text", self._set_tool_redact,
                                      QKeySequence("R"),
                                      "Mark text to remove permanently (black-out)",
                                      checkable=True)
        self.action_redact_apply = act("Appl&y Redaction", self._on_redact_apply,
                                       QKeySequence("Ctrl+Shift+R"),
                                       "Remove the marked text from the document "
                                       "permanently")
        self.action_redact_clear = act("Clear Black-Out &Marks", self._on_redact_clear,
                                       None, "Remove all pending black-out marks")
        self.tool_group = QActionGroup(self)
        for a in (self.action_tool_select, self.action_tool_edit,
                  self.action_tool_add, self.action_tool_redact):
            self.tool_group.addAction(a)
        self.action_tool_select.setChecked(True)

        self.action_zoom_in = act("Zoom &In", self._zoom_in, QKeySequence.ZoomIn, "Zoom in")
        self.action_zoom_out = act("Zoom &Out", self._zoom_out, QKeySequence.ZoomOut,
                                   "Zoom out")
        self.action_fit_width = act("Fit &Width", self._fit_width, QKeySequence("Ctrl+Shift+W"),
                                    "Zoom to fit page width")
        self.action_fit_page = act("Fit &Page", self._fit_page, QKeySequence("Ctrl+0"),
                                   "Zoom to fit whole page")

        self.action_sidebar = act("&Sidebar", self._toggle_sidebar, QKeySequence("Ctrl+B"),
                                  "Show or hide the pages/search sidebar", checkable=True)
        self.action_sidebar.setChecked(True)
        self.action_properties = act("&Properties", self._toggle_properties,
                                     QKeySequence("Ctrl+I"),
                                     "Show or hide the properties panel", checkable=True)
        self.action_properties.setChecked(True)
        self.action_find = act("&Find…", self._focus_search, QKeySequence.Find,
                               "Search in this document")

        self.action_theme_light = act("&Light", lambda: self.theme.apply("light"),
                                      None, "Light theme", checkable=True)
        self.action_theme_dark = act("&Dark", lambda: self.theme.apply("dark"),
                                     None, "Dark theme", checkable=True)
        self.action_theme_system = act("&System", lambda: self.theme.apply("system"),
                                       None, "Follow Windows theme", checkable=True)
        self.theme_group = QActionGroup(self)
        for a in (self.action_theme_light, self.action_theme_dark, self.action_theme_system):
            self.theme_group.addAction(a)
        {"light": self.action_theme_light, "dark": self.action_theme_dark,
         "system": self.action_theme_system}[self.settings.theme].setChecked(True)

        self.action_reduced_motion = act(
            "Reduced &Motion", self._toggle_reduced_motion, None,
            "Minimize animation and transitions", checkable=True)
        self.action_reduced_motion.setChecked(self.settings.reduced_motion)

        self.action_about = act("&About openPDF suite", self._show_about, None, "About this app")
        self.action_limits = act("&Editing Limitations", self._show_limitations, None,
                                 "What openPDF suite can and cannot edit")

        self._save_actions = [self.action_save, self.action_save_as,
                              self.action_undo, self.action_redo]
        self._export_actions = [self.action_convert_to_word]
        self._doc_actions = ([self.action_close_doc, self.action_find]
                             + self._save_actions + self._export_actions)
        self._view_actions = [self.action_zoom_in, self.action_zoom_out,
                              self.action_fit_width, self.action_fit_page]
        self._edit_tool_actions = [self.action_tool_edit, self.action_tool_add,
                                   self.action_tool_redact]
        self._redact_actions = [self.action_redact_apply, self.action_redact_clear]
        # availability is initialized in __init__ after docks exist

    def _has_doc(self) -> bool:
        return self.controller is not None and self.controller.session is not None

    def _update_action_availability(self) -> None:
        has_doc = self._has_doc()
        hint_doc = "Open a document first (Ctrl+O)."
        for a in self._doc_actions + self._view_actions:
            a.setEnabled(has_doc)
            if not has_doc:
                a.setToolTip(hint_doc)
        self.action_tool_select.setEnabled(True)
        self.action_tool_edit.setEnabled(has_doc)
        if not has_doc:
            self.action_tool_edit.setToolTip(hint_doc)
        else:
            self.action_tool_edit.setToolTip(
                "Select and edit existing text (E)")
        self.action_tool_add.setEnabled(has_doc)
        if not has_doc:
            self.action_tool_add.setToolTip(hint_doc)
        else:
            self.action_tool_add.setToolTip("Click an empty spot to add new text (A)")
        self.action_tool_redact.setEnabled(has_doc)
        if not has_doc:
            self.action_tool_redact.setToolTip(hint_doc)
        else:
            self.action_tool_redact.setToolTip(
                "Drag boxes over text to remove it permanently (R)")
        # redaction apply/clear: need a doc AND at least one pending mark
        marks = self.view.has_redact_marks() if self.view is not None else False
        for a in self._redact_actions:
            a.setEnabled(has_doc and marks)
        if has_doc and not marks:
            for a in self._redact_actions:
                a.setToolTip("No black-out marks yet — use the Black Out Text tool (R)")
        elif has_doc:
            self.action_redact_apply.setToolTip(
                "Remove the marked text permanently (Ctrl+Shift+R)")
            self.action_redact_clear.setToolTip("Remove all pending black-out marks")
        if has_doc:
            s = self.controller.session
            self.action_undo.setEnabled(s.can_undo)
            self.action_redo.setEnabled(s.can_redo)
            self.action_save.setEnabled(s.dirty and not self._saving)
            if self._saving:
                self.action_save.setToolTip("Saving…")
            else:
                self.action_save.setToolTip(
                    "Save changes (Ctrl+S)" if s.dirty else "No unsaved changes")
            self.action_save_as.setEnabled(not self._saving)
            self.action_save_as.setToolTip(
                "Saving…" if self._saving
                else "Save changes to a new file (Ctrl+Shift+S)")
            self.action_convert_to_word.setEnabled(not self._converting_docx)
            self.action_convert_to_word.setToolTip(
                "Converting to Word…" if self._converting_docx
                else "Convert the current PDF to a .docx document (Ctrl+Shift+W)")
            self.action_undo.setToolTip(
                "Undo the last edit (Ctrl+Z)" if s.can_undo else "Nothing to undo")
            self.action_redo.setToolTip(
                "Redo the last undone edit (Ctrl+Y)" if s.can_redo else "Nothing to redo")
            self.action_close_doc.setToolTip("Close the current document (Ctrl+W)")
            self.action_find.setToolTip("Search in this document (Ctrl+F)")
        self.sidebar_tabs.search.set_search_enabled(has_doc)
        self.action_doc_info.setEnabled(has_doc)
        self.action_doc_info.setToolTip(
            "Show document information" if has_doc else hint_doc)

    # -- menus ----------------------------------------------------------------
    def _build_menus(self) -> None:
        bar = self.menuBar()

        m_file = bar.addMenu("&File")
        m_file.addAction(self.action_open)
        self.menu_recent = m_file.addMenu("Open &Recent")
        self.menu_recent.aboutToShow.connect(self._populate_recents)
        m_file.addSeparator()
        m_file.addAction(self.action_save)
        m_file.addAction(self.action_save_as)
        m_file.addSeparator()
        m_file.addAction(self.action_convert_to_word)
        m_file.addSeparator()
        m_file.addAction(self.action_close_doc)
        m_file.addAction(self.action_exit)

        m_edit = bar.addMenu("&Edit")
        m_edit.addAction(self.action_undo)
        m_edit.addAction(self.action_redo)
        m_edit.addSeparator()
        m_edit.addAction(self.action_tool_select)
        m_edit.addAction(self.action_tool_edit)
        m_edit.addAction(self.action_tool_add)
        m_edit.addAction(self.action_tool_redact)
        m_edit.addAction(self.action_redact_apply)
        m_edit.addAction(self.action_redact_clear)
        m_edit.addSeparator()
        m_edit.addAction(self.action_find)

        m_view = bar.addMenu("&View")
        m_view.addAction(self.action_zoom_in)
        m_view.addAction(self.action_zoom_out)
        m_view.addAction(self.action_fit_width)
        m_view.addAction(self.action_fit_page)
        m_view.addSeparator()
        m_view.addAction(self.action_sidebar)
        m_view.addAction(self.action_properties)
        m_view.addSeparator()
        m_theme = m_view.addMenu("&Theme")
        m_theme.addAction(self.action_theme_light)
        m_theme.addAction(self.action_theme_dark)
        m_theme.addAction(self.action_theme_system)
        m_view.addAction(self.action_reduced_motion)

        m_doc = bar.addMenu("&Document")
        self.action_doc_info = QAction("Document &Info", self)
        self.action_doc_info.setEnabled(False)
        self.action_doc_info.setToolTip("Open a document first (Ctrl+O).")
        self.action_doc_info.triggered.connect(self._show_doc_info)
        m_doc.addAction(self.action_doc_info)
        m_doc.addAction(self.action_limits)

        m_help = bar.addMenu("&Help")
        m_help.addAction(self.action_about)

    def _populate_recents(self) -> None:
        self.menu_recent.clear()
        recents = self.settings.recent_files()
        if not recents:
            empty = self.menu_recent.addAction("No recent documents")
            empty.setEnabled(False)
            return
        for p in recents:
            a = self.menu_recent.addAction(Path(p).name)
            a.setToolTip(p)
            exists = Path(p).exists()
            a.setEnabled(exists)
            if not exists:
                a.setText(f"{Path(p).name} (missing)")
            a.triggered.connect(lambda _=False, path=p: self.open_document(path))
        self.menu_recent.addSeparator()
        clear = self.menu_recent.addAction("Clear recent list")
        clear.triggered.connect(self._clear_recents)

    def _clear_recents(self) -> None:
        self.settings.clear_recents()
        self.welcome.set_recents([])
        self.statusBar().showMessage("Recent documents list cleared", 3000)

    # -- toolbar -------------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setObjectName("MainToolBar")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)
        self.toolbar = tb

        tb.addAction(self.action_open)
        tb.addAction(self.action_save)
        tb.addSeparator()
        tb.addAction(self.action_undo)
        tb.addAction(self.action_redo)
        tb.addSeparator()
        tb.addAction(self.action_tool_select)
        tb.addAction(self.action_tool_edit)
        tb.addAction(self.action_tool_add)
        tb.addAction(self.action_tool_redact)
        tb.addAction(self.action_redact_apply)
        tb.addSeparator()
        tb.addAction(self.action_zoom_out)
        self.zoom_label = QLabel(" 100% ")
        self.zoom_label.setObjectName("ZoomLabel")
        self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_label.setMinimumWidth(52)
        tb.addWidget(self.zoom_label)
        tb.addAction(self.action_zoom_in)
        tb.addAction(self.action_fit_width)
        tb.addAction(self.action_fit_page)
        tb.addSeparator()
        tb.addAction(self.action_sidebar)
        tb.addAction(self.action_properties)

    # -- central & docks ---------------------------------------------------------
    def _build_central(self) -> None:
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.welcome = WelcomeScreen(self.theme)
        self.welcome.open_requested.connect(self._on_open)
        self.welcome.recent_activated.connect(self.open_document)
        self.stack.addWidget(self.welcome)
        self.welcome.set_recents(self.settings.recent_files())

    def _build_docks(self) -> None:
        self.sidebar_tabs = SidebarTabs(self.theme)
        self.sidebar = QDockWidget("Pages", self)
        self.sidebar.setObjectName("SidebarDock")
        self.sidebar.setWidget(self.sidebar_tabs)
        self.sidebar.setFeatures(QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.sidebar)
        self.sidebar.setMaximumWidth(300)
        self.sidebar.setMinimumWidth(200)
        self.sidebar_tabs.thumbnails.page_requested.connect(self._goto_page)

        self.properties = PropertiesPanel(self.theme)
        self.props_dock = QDockWidget("Properties", self)
        self.props_dock.setObjectName("PropertiesDock")
        self.props_dock.setWidget(self.properties)
        self.props_dock.setFeatures(QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.RightDockWidgetArea, self.props_dock)
        self.props_dock.setMaximumWidth(320)
        self.props_dock.setMinimumWidth(240)

        self.sidebar_tabs.search.search_requested.connect(self._on_search_requested)
        self.sidebar_tabs.search.result_activated.connect(self._on_search_result)
        self.sidebar_tabs.search.blackout_all_requested.connect(
            self._on_blackout_all_search)
        r = self.sidebar_tabs.redact
        r.mark_activated.connect(self._on_search_result)
        r.mark_remove_requested.connect(self._on_redact_mark_removed)
        r.clear_requested.connect(self._on_redact_clear)

        p = self.properties
        p.font_change_requested.connect(
            lambda s: self._style_overrides.update(font=s or None))
        p.size_change_requested.connect(
            lambda v: self._style_overrides.update(size=float(v)))
        p.color_change_requested.connect(self._on_color_override)
        p.alignment_change_requested.connect(
            lambda s: self._style_overrides.update(align=s))
        p.bold_toggled.connect(lambda b: self._style_overrides.update(bold=bool(b)))
        p.italic_toggled.connect(lambda i: self._style_overrides.update(italic=bool(i)))

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self.page_label = QLabel("No document")
        sb.addWidget(self.page_label)
        self.zoom_status = QLabel("")
        sb.addPermanentWidget(self.zoom_status)
        self.mode_label = QLabel("Select")
        sb.addPermanentWidget(self.mode_label)
        sb.setSizeGripEnabled(True)

    # -- theme ------------------------------------------------------------------
    def _retheme(self, *_args) -> None:
        pairs = [
            (self.action_open, "open"), (self.action_save, "save"),
            (self.action_save_as, "save_as"), (self.action_undo, "undo"),
            (self.action_redo, "redo"), (self.action_tool_select, "select"),
            (self.action_tool_edit, "edit_text"), (self.action_tool_add, "add_text"),
            (self.action_zoom_in, "zoom_in"), (self.action_zoom_out, "zoom_out"),
            (self.action_fit_width, "fit_width"), (self.action_fit_page, "fit_page"),
            (self.action_sidebar, "sidebar"), (self.action_properties, "properties"),
            (self.action_find, "search"),
            (self.action_convert_to_word, "convert"),
        ]
        for action, name in pairs:
            action.setIcon(self.theme.icon(name, 18))
        self.sidebar_tabs.search.button.setIcon(self.theme.icon("search", 16))
        self.setWindowTitle(self._title())

    def _title(self) -> str:
        if self._current_path:
            dirty = " •" if (self.controller and self.controller.session
                             and self.controller.session.dirty) else ""
            return f"{self._current_path.name}{dirty} — openPDF suite"
        return "openPDF suite"

    def _refresh_title(self) -> None:
        self.setWindowTitle(self._title())

    # -- open flow ----------------------------------------------------------------
    def _on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", self.settings.last_open_dir,
            "PDF documents (*.pdf);;All files (*)"
        )
        if path:
            self.open_document(path)

    def open_document(self, path: str) -> None:
        p = Path(path)
        if not p.exists():
            self.settings.remove_recent(str(p))
            self.welcome.set_recents(self.settings.recent_files())
            QMessageBox.warning(self, "File not found",
                                f"The file no longer exists:\n{p}")
            return
        if not _looks_like_pdf(p):
            QMessageBox.warning(self, "Not a PDF",
                                f"This file does not look like a PDF:\n{p.name}")
            return
        if self.controller is not None:
            self._teardown_controller()
        self._current_path = p
        self.settings.push_recent(str(p))
        self.settings.last_open_dir = str(p.parent)
        self.welcome.set_recents(self.settings.recent_files())

        self._show_central(LoadingWidget(self.theme, p))
        self.page_label.setText(f"Opening {p.name}…")
        self._refresh_title()

        self.controller = DocumentController(parent=self)
        c = self.controller
        c.document_opened.connect(self._on_document_opened)
        c.needs_password.connect(self._on_needs_password)
        c.thumbnail_rendered.connect(self._on_thumbnail)
        c.search_done.connect(self._on_search_done)
        c.edit_prepared.connect(self._on_edit_prepared)
        c.edit_committed.connect(self._on_edit_committed)
        c.undo_redo_done.connect(self._on_undo_redo_done)
        c.saved.connect(self._on_saved)
        c.failure.connect(self._on_failure)
        c.snapshot_ready.connect(self._on_snapshot_ready)
        c.save_failed.connect(self._on_save_failed)
        c.docx_exported.connect(self._on_docx_exported)
        c.worker_crashed.connect(self._on_worker_crashed)
        self._recovery_key = None  # fresh open: recovery keyed by doc_id
        self._update_action_availability()
        c.open(p)
        log.info("opening document path=%s", p)

    def _on_document_opened(self, meta, page_sizes) -> None:
        c = self.controller
        if c is None or c.session is None:
            return
        if getattr(meta, "is_signed", False):
            self.statusBar().showMessage(
                "This document carries a digital signature. Saving edits will "
                "invalidate it — use Save As to keep the signed original.", 9000)
        self.view = DocumentView(self.theme)
        self._show_central(self.view)
        self.view.view_request_page.connect(self._request_page)
        self.view.view_request_lines.connect(self._request_lines)
        self.view.current_page_changed.connect(self._on_page_changed)
        self.view.zoom_changed.connect(self._on_zoom_changed)
        self.view.status_message.connect(lambda m: self.statusBar().showMessage(m, 4000))
        c.page_rendered.connect(self.view.on_page_rendered)
        c.page_lines_ready.connect(self.view.on_page_lines)
        c.regions_ready.connect(self.view.on_regions)
        self.view.view_request_regions.connect(self._request_regions)
        self.view.region_clicked.connect(self._on_region_clicked)
        self.view.region_double_clicked.connect(self._on_region_double_clicked)
        self.view.region_box_changed.connect(self._on_region_box_changed)
        self.view.add_text_requested.connect(self._on_add_text_requested)
        self.view.redact_marks_changed.connect(self._on_redact_marks_changed)
        self.view.set_document(page_sizes, c.session.page_rotations)

        self.sidebar_tabs.thumbnails.set_page_count(meta.page_count)
        c.request_thumbnails(list(range(meta.page_count)))

        self.view.fit_width()
        self._on_zoom_changed(self.view.zoom)
        self._on_page_changed(0)
        self._update_action_availability()
        self._refresh_title()

        notes = []
        if meta.is_signed:
            notes.append("contains a digital signature (editing changes its validity)")
        if meta.is_encrypted:
            notes.append("is password-protected")
        suffix = f" — {', and '.join(notes)}" if notes else ""
        self.statusBar().showMessage(
            f"Opened {self._current_path.name}: {meta.page_count} pages{suffix}", 6000)

    def _on_needs_password(self, path: str) -> None:
        text, ok = QInputDialog.getText(
            self, "Password required",
            f"“{Path(path).name}” is password-protected.\nEnter the password:",
            QLineEdit.Password,
        )
        if ok and text:
            self.controller.open(path, password=text)
        else:
            self._on_close_doc()
            self.statusBar().showMessage("Open canceled — password required.", 5000)

    def _show_central(self, widget: QWidget) -> None:
        while self.stack.count() > 1:
            w = self.stack.widget(1)
            self.stack.removeWidget(w)
            w.deleteLater()
        self.stack.addWidget(widget)
        self.stack.setCurrentIndex(1)

    def _teardown_controller(self) -> None:
        self._clear_selection_state()
        self._saved_once = False
        self._saving = False
        self._style_overrides = {}
        if self.controller is not None:
            self.controller.shutdown()
            self.controller.deleteLater()
            self.controller = None
        self.view = None
        self.sidebar_tabs.thumbnails.clear_pages()
        self.sidebar_tabs.search.clear_results()
        self.properties.clear_style()

    # -- controller signal handlers ---------------------------------------------
    def _request_page(self, page: int, zoom: float, dpr: float) -> None:
        if self.controller and self.controller.session:
            self.controller.request_page(page, zoom, dpr)

    def _request_lines(self, page: int) -> None:
        if self.controller and self.controller.session:
            self.controller.request_page_lines(page)

    def _on_thumbnail(self, page: int, pixmap) -> None:
        self.sidebar_tabs.thumbnails.set_thumbnail(page, QIcon(pixmap))

    def _on_page_changed(self, page: int) -> None:
        if self.controller and self.controller.session:
            n = self.controller.session.page_count
            self.page_label.setText(f"Page {page + 1} of {n}")
            self.controller.session.current_page = page
            self.sidebar_tabs.thumbnails.select_page(page)

    def _on_zoom_changed(self, zoom: float) -> None:
        text = f"{zoom * 100:.0f}%"
        self.zoom_label.setText(f" {text} ")
        self.zoom_status.setText(text)

    def _on_failure(self, message: str) -> None:
        log.error("document failure: %s", message.splitlines()[0])
        self.statusBar().showMessage(message.splitlines()[0], 8000)

    def _on_worker_crashed(self, message: str) -> None:
        log.error("worker crashed: %s", message)
        path = self._current_path
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("PDF engine stopped")
        box.setText("The PDF worker process stopped unexpectedly.")
        box.setInformativeText(
            "Your file on disk was not modified. Unsaved edits in this session "
            "are lost." + ("\n\nReopen the document?" if path else ""))
        reopen = box.addButton("Reopen document", QMessageBox.AcceptRole) if path else None
        box.addButton(QMessageBox.Close)
        box.exec()
        self._teardown_controller()
        self._current_path = None
        self.stack.setCurrentIndex(0)
        self.page_label.setText("No document")
        self._update_action_availability()
        self._refresh_title()
        if reopen is not None and box.clickedButton() is reopen and path:
            self.open_document(str(path))

    # -- editing ----------------------------------------------------------------
    def _request_regions(self, page: int) -> None:
        if self.controller and self.controller.session:
            self.controller.request_regions(page)

    def _on_region_clicked(self, page: int, region) -> None:
        if not region.editable:
            self._selected_region = None
            self._clear_selection_state()
            self.statusBar().showMessage(region.unsupported_reason, 8000)
            return
        self._selected_region = (page, region)
        self._style_overrides = {}
        if self.view:
            self.view.set_selected_region(page, region.bbox.as_tuple())
        dom = region.dominant_font
        hex_color = f"#{int(dom.color.r * 255):02X}{int(dom.color.g * 255):02X}{int(dom.color.b * 255):02X}"
        note = ""
        if dom.is_subset:
            note = ("Original font is an embedded subset — it may lack the characters "
                    "you type. The preview shows any substitution before you apply.")
        self.properties.show_style(dom.family, dom.size, dom.bold, dom.italic,
                                   hex_color, note)
        text = region.text.strip()
        shown = text[:38] + "…" if len(text) > 38 else text
        self.statusBar().showMessage(
            f"Selected “{shown}” — double-click to edit", 5000)
        if region.mode == EditMode.REFLOW_BOX:
            if self._layout_box and self._layout_box[2] == region.region_id:
                box = self._layout_box[1]
            else:
                pb = region.paragraph_box or region.bbox
                box = pb.as_tuple()
                self._layout_box = (page, box, region.region_id)
            self.view.set_layout_box(page, box)
            self.statusBar().showMessage(
                "Paragraph selected — drag handles to resize the reflow box, "
                "drag inside to move it, double-click to edit.", 6000)
        else:
            self._layout_box = None
            self.view.set_layout_box(None, None)
            if region.grouping_reason:
                self.statusBar().showMessage(
                    f"Selected “{shown}” — {region.grouping_reason}", 8000)

    def _on_region_double_clicked(self, page: int, region) -> None:
        if not region.editable:
            QMessageBox.information(
                self, "This text cannot be edited yet",
                region.unsupported_reason + "\n\n"
                "You can still read, search, copy, and print this document.")
            return
        self._open_editor(page, region)

    def _on_region_box_changed(self, page: int, box: tuple) -> None:
        if self._layout_box and self._layout_box[0] == page:
            self._layout_box = (page, box, self._layout_box[2])
            self.statusBar().showMessage(
                f"Reflow box {box[2] - box[0]:.0f} × {box[3] - box[1]:.0f} pt. "
                "The source removal area stays unchanged.", 5000)

    def _enlarged_box(self, pending: dict, validation) -> tuple | None:
        """Grow the layout box downward, clipped to the next existing text."""
        region = pending["region"]
        page = pending["page"]
        sizes = self.controller.session.page_sizes
        if not sizes or not (0 <= page < len(sizes)):
            return None
        if self._layout_box and self._layout_box[2] == region.region_id:
            x0, y0, x1, y1 = self._layout_box[1]
        else:
            b = region.paragraph_box or region.bbox
            x0, y0, x1, y1 = b.as_tuple()
        # nearest text below (excluding the region's own lines)
        limit = sizes[page][1] - 18.0
        for _text, bbox, _size in self.view.page_lines.get(page, []):
            if bbox[1] > y1 + 1.0 and bbox[0] < x1 and bbox[2] > x0:
                limit = min(limit, bbox[1] - 2.0)
        extra = validation.overflow_px * 1.15 + 4.0
        new_bottom = min(y1 + extra, limit)
        if new_bottom < y1 + 6.0:
            return None
        return (x0, y0, x1, new_bottom)

    def _open_editor(self, page: int, region, preset: str | None = None) -> None:
        if self.view is None or not (0 <= page < len(self.view.frames)):
            return
        self._close_overlay()
        frame = self.view.frames[page]
        pr = frame.page_rect()
        # the overlay floats over the rendered page (display space); region
        # geometry is engine space
        disp = self.view.rect_to_display(page, region.bbox)
        ox = (frame.x() + pr.x() + disp.x0 * frame.zoom
              - self.view.horizontalScrollBar().value())
        oy = (frame.y() + pr.y() + disp.y0 * frame.zoom
              - self.view.verticalScrollBar().value())
        dom = region.dominant_font
        multiline = region.mode == EditMode.REFLOW_BOX
        if preset is not None:
            text = preset
        elif multiline:
            text = "\n".join(ln.text for ln in region.lines)
        else:
            text = region.text
        overlay = TextOverlay(
            self.view.viewport(), self.theme, ox, oy,
            max(disp.width * frame.zoom, 200),
            text, dom.family, dom.size * frame.zoom, multiline=multiline)
        overlay.applied.connect(self._submit_edit)
        overlay.cancelled.connect(self._on_overlay_cancelled)
        self._overlay = overlay
        self._selected_region = (page, region)
        overlay.show()

    def _close_overlay(self) -> None:
        if self._overlay is not None:
            self._overlay.close()
            self._overlay.deleteLater()
            self._overlay = None

    def _on_overlay_cancelled(self) -> None:
        self._overlay = None
        self._pending_edit = None
        self.statusBar().showMessage("Editing canceled.", 3000)

    def _on_color_override(self, qcolor: QColor) -> None:
        self._style_overrides["color"] = Color(
            qcolor.redF(), qcolor.greenF(), qcolor.blueF())

    def _submit_edit(self, text: str, auto_shrink: bool = False) -> None:
        if self._selected_region is None or self.controller is None:
            return
        if self._overlay is not None:
            self._overlay = None  # overlay closes itself on apply
        page, region = self._selected_region
        if not text.strip():
            QMessageBox.information(
                self, "Empty replacement",
                "The replacement is empty. Whole-region deletion is not supported "
                "yet — type the new text, or cancel with Esc.")
            return
        ov = self._style_overrides
        target = None
        if region.mode == EditMode.REFLOW_BOX:
            if self._layout_box and self._layout_box[2] == region.region_id:
                target = Rect(*self._layout_box[1])
            else:
                target = region.paragraph_box or region.bbox
        edit = ReplacementEdit(
            region_id=region.region_id,
            source_revision=self.controller.session.revision,
            new_text=text,
            mode=region.mode,
            target_box=target,
            font_override=ov.get("font"),
            size_override=ov.get("size"),
            color_override=ov.get("color"),
            bold_override=ov.get("bold"),
            italic_override=ov.get("italic"),
            alignment=_ALIGN_FROM_STR.get(ov.get("align", "left"), TextAlignment.LEFT),
            auto_shrink=auto_shrink,
            page_index=page,
        )
        self._pending_edit = {"page": page, "region": region, "text": text}
        self.statusBar().showMessage("Preparing replacement…", 3000)
        self.controller.prepare_edit(page, region, edit)

    def _on_edit_prepared(self, info: dict) -> None:
        if getattr(self, "_pending_redact", None) is not None:
            # black-out flow bypasses overflow/replacement handling
            self._on_redaction_prepared(info)
            return
        pending = self._pending_edit
        if pending is None:
            return
        if not info["ok"] and pending.get("region") is None:
            validation = info.get("validation")
            if validation is not None and validation.overflowed:
                self._on_insert_prepared(info)
                return
        if info["ok"]:
            if self._confirm_preview(info):
                self.controller.commit_edit(info["prepare_key"])
            else:
                self._pending_edit = None
                self.statusBar().showMessage(
                    "Edit canceled — nothing was changed.", 4000)
            return
        validation = info.get("validation")
        if validation is not None and validation.overflowed:
            region = pending.get("region")
            can_enlarge = (region is not None
                           and region.mode == EditMode.REFLOW_BOX
                           and pending.get("enlarges", 0) < 2)
            choice = self._ask_overflow(validation, can_enlarge_box=can_enlarge)
            if choice == "enlarge":
                new_box = self._enlarged_box(pending, validation)
                if new_box is None:
                    self._pending_edit = None
                    QMessageBox.warning(
                        self, "Not enough room on the page",
                        "The text box cannot grow further without overlapping "
                        "other content. Reduce the font size, shorten the text, "
                        "or cancel.")
                    return
                self._layout_box = (pending["page"], new_box, region.region_id)
                self.view.set_layout_box(pending["page"], new_box)
                retry = dict(pending)
                retry["enlarges"] = pending.get("enlarges", 0) + 1
                self._pending_edit = retry
                self._submit_edit(pending["text"])
                return
            if choice == "shrink":
                self._submit_edit(pending["text"], auto_shrink=True)
                return
            if choice == "shorten":
                self._open_editor(pending["page"], pending["region"],
                                  preset=pending["text"])
                return
            self._pending_edit = None
            self.statusBar().showMessage("Edit canceled — nothing was changed.", 4000)
            return
        self._pending_edit = None
        QMessageBox.warning(self, "Edit could not be applied",
                            "\n".join(info.get("issues") or ["Unknown issue."]))

    def _confirm_preview(self, info: dict) -> bool:
        """Show the PDF-engine before/after preview. Seam for UI tests."""
        pending = self._pending_edit
        if pending is None:
            return False
        before_png, after_png = info.get("before_png"), info.get("preview_png")
        validation = info.get("validation")
        if not before_png or not after_png:
            return QMessageBox.question(
                self, "Apply edit",
                "Preview images are unavailable, but the edit passed text and "
                "pixel validation. Apply it?") == QMessageBox.Yes
        region = pending.get("region")
        if region is not None:
            # the preview dialog highlights on rendered (display) images
            box = self.view.rect_to_display(pending["page"], region.bbox)
            pad_x = max(10.0, box.width * 0.4)
        else:
            x0, y0, x1, y1 = pending["box"]
            box = self.view.rect_to_display(pending["page"], Rect(x0, y0, x1, y1))
            pad_x = 10.0
        changed = Rect(box.x0 - 8, box.y0 - 5, box.x1 + pad_x, box.y1 + 5)
        dlg = PreviewDialog(self, self.theme, before_png, after_png, changed,
                            2.0, validation,
                            list(validation.issues) if validation else [])
        return dlg.exec() == QDialog.Accepted

    def _ask_overflow(self, validation, can_enlarge_box: bool = False) -> str:
        """Offer explicit overflow choices (§9). Seam for UI tests."""
        dlg = OverflowDialog(self, self.theme, validation.required_size,
                             validation.overflow_px,
                             can_enlarge_box=can_enlarge_box)
        dlg.exec()
        return dlg.choice

    def _on_edit_committed(self, revision: int, page: int) -> None:
        if getattr(self, "_pending_redact", None) is not None \
                and self._pending_redact.get("page") == page:
            self._on_redaction_committed(page, revision)
            self._update_action_availability()
            return
        was_insert = self._pending_edit is not None             and self._pending_edit.get("region") is None
        self._pending_edit = None
        self._recovery_timer.start()
        self._selected_region = None
        self._insert_ctx = None
        if self.view:
            self.view.refresh_after_edit(page)
            if was_insert:
                # stay in Add mode so several blocks can be placed in a row
                self.view.set_layout_box(None, None)
        self._update_action_availability()
        self._refresh_title()
        self.statusBar().showMessage(
            f"Edit applied (revision {revision}). Ctrl+Z to undo.", 5000)

    def _on_undo_redo_done(self, revision, can_undo, can_redo, page) -> None:
        self._pending_edit = None
        self._selected_region = None
        if self.view:
            self.view.refresh_after_edit(page)
        self._update_action_availability()
        self._refresh_title()
        self.statusBar().showMessage(f"Now at revision {revision}.", 3000)

    # -- saving --------------------------------------------------------------------
    def _on_save(self) -> None:
        if not self._has_doc() or self._saving:
            return
        if not self._saved_once:
            # §12: Save As is the first-save default for an edited document
            self._on_save_as()
            return
        self._last_save_dest = str(self.controller.session.path)
        self._start_save(self._last_save_dest)

    def _on_save_as(self) -> None:
        if not self._has_doc() or self._saving:
            return
        s = self.controller.session
        start = str(s.path) if s.path else self.settings.last_open_dir
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save PDF As", start, "PDF documents (*.pdf)")
        if dest:
            self._last_save_dest = dest
            self._start_save(dest)

    def _start_save(self, dest: str) -> None:
        """Kick off an async save and show that it is in progress."""
        self._saving = True
        self._update_action_availability()
        name = Path(dest).name
        self.statusBar().showMessage(f"Saving {name}… (validating before replacing)")
        self.controller.save(dest)

    def _on_saved(self, path: str, fingerprint: str) -> None:
        self._saved_once = True
        self._saving = False
        self._recovery_timer.stop()
        if self.controller and self.controller.session:
            self.recovery.discard(self._recovery_key
                                  or self.controller.session.doc_id)
            self._recovery_key = None
        self.settings.push_recent(path)
        self.settings.last_open_dir = str(Path(path).parent)
        self._current_path = Path(path)
        self.welcome.set_recents(self.settings.recent_files())
        self._update_action_availability()
        self._refresh_title()
        self.statusBar().showMessage(
            f"Saved {Path(path).name} — output validated before replacing.", 6000)

    def _on_save_failed(self, message: str) -> None:
        log.error("save failed: %s", message.splitlines()[0])
        self._saving = False
        self._update_action_availability()
        self.statusBar().showMessage("Save failed — the original file was not modified.", 8000)
        if self.controller and self.controller.session:
            # keep the session alive: the working copy is untouched
            self._saved_once = False
        if self._show_save_failure(message) == "unencrypted" \
                and self.controller and self._last_save_dest:
            self.controller.save(self._last_save_dest, encryption="remove")

    def _show_save_failure(self, message: str) -> str:
        """Explain a failed save. Seam for UI tests; returns 'unencrypted'."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Save failed")
        box.setText("The document could not be saved.")
        box.setInformativeText(
            message + "\n\nYour original file and unsaved edits are intact.")
        if "encryption" in message.lower() and self._last_save_dest:
            unenc = box.addButton("Save an unencrypted copy",
                                  QMessageBox.AcceptRole)
        else:
            unenc = None
        box.addButton("OK", QMessageBox.RejectRole)
        box.exec()
        return "unencrypted" if unenc is not None and box.clickedButton() is unenc \
            else "ok"

    # -- export (PDF -> DOCX) --------------------------------------------------
    def _on_convert_to_word(self) -> None:
        if not self._has_doc() or self._saving:
            return
        s = self.controller.session
        start = str(s.path) if s.path else self.settings.last_open_dir
        # suggest a sibling .docx next to the source PDF
        suggestion = ""
        if s.path:
            suggestion = str(s.path.with_suffix(".docx"))
        dest, _ = QFileDialog.getSaveFileName(
            self, "Convert to Word", suggestion or start,
            "Word documents (*.docx)")
        if not dest:
            return
        self._converting_docx = True
        self._update_action_availability()
        name = Path(dest).name
        self.statusBar().showMessage(f"Converting {name} to Word…")
        self.controller.export_docx(dest, self.settings.docx_options())

    def _on_docx_exported(self, result: DocxExportResult) -> None:
        self._converting_docx = False
        self._update_action_availability()
        path = result.output_path
        if not path:
            self.statusBar().showMessage("Conversion to Word failed.", 8000)
            QMessageBox.warning(self, "Convert to Word",
                                "The conversion did not produce a file.")
            return
        name = Path(path).name
        n = len(result.unsupported_items)
        if n:
            msg = (f"Saved {name} — {result.pages_written} pages written; "
                   f"{n} item(s) were skipped because Word cannot exact")
            self.statusBar().showMessage(msg, 6000)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("Converted to Word")
            box.setText(f"Saved {name}.")
            box.setInformativeText(
                f"{result.pages_written} page(s) written.\n"
                f"{n} item(s) were skipped during conversion. "
                "Click Show details to see what was skipped.")
            details = "\n".join(
                f"• Page {u.page_index + 1}: {u.message}" for u in result.unsupported_items
            ) or "No skipped items."
            box.setDetailedText(details)
            open_btn = box.addButton("Open file", QMessageBox.AcceptRole)
            folder_btn = box.addButton("Open folder", QMessageBox.ActionRole)
            box.addButton("Close", QMessageBox.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is open_btn:
                self._open_path_in_os(path)
            elif clicked is folder_btn:
                self._open_path_in_os(str(Path(path).parent))
        else:
            self.statusBar().showMessage(
                f"Saved {name} — {result.pages_written} page(s) written.", 6000)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("Converted to Word")
            box.setText(f"Saved {name}.")
            box.setInformativeText(
                f"{result.pages_written} page(s) written. "
                "Open the file in Word or LibreOffice to review.")
            open_btn = box.addButton("Open file", QMessageBox.AcceptRole)
            folder_btn = box.addButton("Open folder", QMessageBox.ActionRole)
            box.addButton("Close", QMessageBox.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is open_btn:
                self._open_path_in_os(path)
            elif clicked is folder_btn:
                self._open_path_in_os(str(Path(path).parent))

    def _open_path_in_os(self, path: str) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            self.statusBar().showMessage(f"Could not open {path}: {exc}", 6000)

    # -- recovery (§11) -------------------------------------------------------
    def _write_recovery(self) -> None:
        if self.controller and self.controller.session and                 self.controller.session.dirty:
            self.controller.snapshot()

    def _on_snapshot_ready(self, doc_id: str, revision: int, data: bytes,
                           path: str) -> None:
        sess = self.controller.session if self.controller else None
        if sess is None or doc_id != sess.doc_id or not sess.dirty:
            return
        key = self._recovery_key or doc_id
        self.recovery.write(key, str(sess.path or ""), sess.fingerprint,
                            sess.revision, sess.saved_revision, data)

    def _check_recovery(self) -> None:
        """Offer restoring an interrupted session at startup."""
        if self._has_doc():
            return
        entries = self.recovery.list()
        if not entries:
            return
        entry = entries[0]  # most recent
        name = Path(entry.source_path).name if entry.source_path else "untitled"
        stale_note = (f"\n\nNote: {entry.stale_reason}." if entry.stale else "")
        choice = self._ask_recovery(entry, name, stale_note)
        if choice == "restore":
            self._restore_recovery(entry)
        elif choice == "discard":
            self.recovery.discard(entry.session_id)

    def _ask_recovery(self, entry, name: str, stale_note: str) -> str:
        """Startup recovery prompt. Seam for UI tests."""
        box = QMessageBox(self)
        box.setWindowTitle("Recover unsaved work")
        box.setIcon(QMessageBox.Question)
        box.setText(
            f"openPDF suite found an unsaved session for “{name}” "
            f"(revision {entry.revision}) from "
            f"{entry.timestamp[:19].replace('T', ' ')} UTC.{stale_note}\n\n"
            "The original file on disk has not been touched.")
        restore = box.addButton("Restore", QMessageBox.AcceptRole)
        discard = box.addButton("Discard", QMessageBox.DestructiveRole)
        box.addButton("Not now", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is restore:
            return "restore"
        if box.clickedButton() is discard:
            return "discard"
        return "ignore"

    def _restore_recovery(self, entry) -> None:
        data = self.recovery.load_bytes(entry.session_id)
        self._recovery_key = entry.session_id
        if data is None:
            QMessageBox.warning(self, "Recovery failed",
                                "The recovery data could not be read.")
            return
        self._saved_once = False
        self._current_path = Path(entry.source_path) if entry.source_path else None
        self._show_central(LoadingWidget(self.theme, self._current_path
                                         or Path("recovered session")))
        self.page_label.setText("Restoring recovered session…")
        self._refresh_title()
        self.controller = DocumentController(parent=self)
        c = self.controller
        c.document_opened.connect(self._on_document_opened)
        c.needs_password.connect(self._on_needs_password)
        c.thumbnail_rendered.connect(self._on_thumbnail)
        c.search_done.connect(self._on_search_done)
        c.edit_prepared.connect(self._on_edit_prepared)
        c.edit_committed.connect(self._on_edit_committed)
        c.undo_redo_done.connect(self._on_undo_redo_done)
        c.saved.connect(self._on_saved)
        c.failure.connect(self._on_failure)
        c.worker_crashed.connect(self._on_worker_crashed)
        c.snapshot_ready.connect(self._on_snapshot_ready)
        c.save_failed.connect(self._on_save_failed)
        self._update_action_availability()
        c.open_bytes(data, entry.source_path, entry.fingerprint, entry.revision)
        if self._current_path:
            self.settings.push_recent(str(self._current_path))
            self.welcome.set_recents(self.settings.recent_files())

    def _on_undo(self) -> None:
        if self.controller and self.controller.session and self.controller.session.can_undo:
            self.controller.undo()

    def _on_redo(self) -> None:
        if self.controller and self.controller.session and self.controller.session.can_redo:
            self.controller.redo()

    # -- tools & view ------------------------------------------------------------
    def _set_search_blackout(self, visible: bool) -> None:
        self.sidebar_tabs.search.set_blackout_visible(visible)

    def _set_tool_select(self) -> None:
        if self.view:
            self.view.set_mode("select")
        self._clear_selection_state()
        self._set_search_blackout(False)
        self.mode_label.setText("Select")

    def _set_tool_edit(self) -> None:
        if self.view:
            self.view.set_mode("edit")
        self._set_search_blackout(False)
        self.mode_label.setText("Edit Text")
        self.statusBar().showMessage(
            "Edit Text: hover a line, click to select, double-click to edit.", 6000)

    def _set_tool_add(self) -> None:
        if self.view:
            self.view.set_mode("add")
        self._set_search_blackout(False)
        self.mode_label.setText("Add Text")
        self.statusBar().showMessage(
            "Add Text: click an empty area of the page where new text goes.", 6000)

    def _set_tool_redact(self) -> None:
        if self.view is not None:
            self.view.set_mode("redact")
        self._set_search_blackout(True)
        self.sidebar_tabs.setCurrentWidget(self.sidebar_tabs.redact)
        self.mode_label.setText("Black Out")
        self.statusBar().showMessage(
            "Black Out: drag a box over text to mark it, click a mark to remove it. "
            "Apply Redaction removes marked text permanently.", 8000)

    def _on_redact_clear(self) -> None:
        if self.view is not None:
            self.view.clear_redact_marks()
            self.statusBar().showMessage("Black-out marks cleared.", 4000)

    def _on_blackout_all_search(self, hits: list) -> None:
        """Turn search hits into black-out marks (engine-space rects)."""
        if self.view is None:
            return
        from ..domain.models import Rect as _Rect
        for page, rect in hits:
            self.view.add_redact_mark(page, _Rect(*rect))
        self.sidebar_tabs.search.set_blackout_visible(True)
        n = len(hits)
        self.statusBar().showMessage(
            f"{n} search hit{'s' if n != 1 else ''} added as black-out marks — "
            "review them, then Apply Redaction.", 8000)

    def _on_redact_mark_removed(self, page: int, rect) -> None:
        if self.view is not None:
            from ..domain.models import Rect as _Rect
            self.view.remove_redact_mark(page, _Rect(*rect.as_tuple())
                                         if hasattr(rect, "as_tuple") else _Rect(*rect))

    def _on_redact_marks_changed(self) -> None:
        self._refresh_redact_panel()
        self._update_action_availability()

    def _refresh_redact_panel(self) -> None:
        """Mirror the view's marks into the sidebar list, with text snippets."""
        redact_panel = self.sidebar_tabs.redact
        if self.view is None:
            redact_panel.show_marks([])
            return
        items: list[tuple[int, str, object]] = []
        for page, rect in self.view.get_redact_marks():
            snippet = self._snippet_under(page, rect)
            items.append((page, snippet, rect))
        redact_panel.show_marks(items)

    def _snippet_under(self, page: int, rect) -> str:
        """Text of the lines a mark covers, for the review list."""
        lines = self.view.page_lines.get(page, []) if self.view else []
        parts: list[str] = []
        for text, bbox, _baseline in lines:
            b = tuple(bbox)
            if not (b[2] < rect.x0 or rect.x1 < b[0] or b[3] < rect.y0 or rect.y1 < b[3]):
                parts.append(text.strip())
        snippet = " ".join(parts)
        if len(snippet) > 60:
            snippet = snippet[:57] + "…"
        return snippet or "(no text found here)"

    def _on_redact_apply(self) -> None:
        """Apply all pending black-out marks, one page at a time.

        Sequential per-page prepare → preview → commit; cancel aborts the rest.
        """
        if self.view is None or self.controller is None:
            return
        marks = self.view.get_redact_marks()
        if not marks:
            return
        pages = sorted({p for p, _r in marks})
        self._redact_queue = [
            (p, [r for pp, r in marks if pp == p]) for p in pages
        ]
        self._redact_done: list[int] = []
        self.statusBar().showMessage("Preparing redaction…", 4000)
        self._apply_next_redaction()

    def _apply_next_redaction(self) -> None:
        if self.controller is None:
            self._redact_queue = []
            return
        if not self._redact_queue:
            self.view.clear_redact_marks()
            self._refresh_title()
            done = len(self._redact_done)
            self.statusBar().showMessage(
                f"Redaction applied on {done} page{'s' if done != 1 else ''} — "
                "the removed text is gone from the document. Save to keep it.",
                9000)
            return
        page, boxes = self._redact_queue[0]
        self._pending_redact = {"page": page, "boxes": boxes}
        self.controller.prepare_redact(page, boxes)

    def _on_redaction_prepared(self, info: dict) -> None:
        pending = getattr(self, "_pending_redact", None)
        if pending is None:
            return
        if not info["ok"]:
            self._redact_queue = []
            self._pending_redact = None
            issues = info.get("issues") or ["The redaction could not be applied."]
            QMessageBox.warning(self, "Black Out rejected", "\n".join(issues))
            return
        self._pending_redact["key"] = info["prepare_key"]
        if self._confirm_redaction_preview(info):
            self.controller.commit_edit(info["prepare_key"])
        else:
            self._redact_queue = []
            self._pending_redact = None
            self.statusBar().showMessage(
                "Redaction canceled — nothing was changed.", 5000)

    def _on_redaction_committed(self, page: int, revision: int) -> None:
        pending = getattr(self, "_pending_redact", None)
        if pending is None or pending.get("page") != page:
            return
        self._pending_redact = None
        self._redact_done.append(page)
        self._redact_queue = self._redact_queue[1:]
        self.view.refresh_after_edit(page)
        self._apply_next_redaction()

    def _confirm_redaction_preview(self, info: dict) -> bool:
        """Before/after preview for a black-out page. Seam for UI tests."""
        before_png, after_png = info.get("before_png"), info.get("preview_png")
        validation = info.get("validation")
        pending = self._pending_redact or {}
        if not before_png or not after_png:
            return QMessageBox.question(
                self, "Apply redaction",
                "Preview images are unavailable, but the redaction passed "
                "validation. Apply it?") == QMessageBox.StandardButton.Yes
        boxes = list(pending.get("boxes") or [])  # already engine-space Rects
        if boxes:
            change = boxes[0]
            for r in boxes[1:]:
                change = Rect(min(change.x0, r.x0), min(change.y0, r.y0),
                              max(change.x1, r.x1), max(change.y1, r.y1))
            disp = self.view.rect_to_display(pending["page"], change)
        else:
            disp = Rect(0, 0, 1, 1)
        dlg = PreviewDialog(self, self.theme, before_png, after_png, disp,
                            2.0, validation, info.get("issues") or [],
                            apply_label="Apply redaction",
                            title="Preview black-out")
        return dlg.exec() == QDialog.Accepted

    def _on_add_text_requested(self, page: int, x_pt: float, y_pt: float) -> None:
        if self.view is None or not (0 <= page < len(self.view.frames)):
            return
        self._close_overlay()
        # default box: 260 pt wide, 3 lines tall, top-left at the click.
        # The click arrives in engine space; clamp against engine page dims.
        ew, _eh = self.view.page_size_engine(page)
        box = (x_pt, y_pt, min(x_pt + 260.0, ew - 36.0), y_pt + 48.0)
        self._insert_ctx = {"page": page, "box": box}
        self.view.set_layout_box(page, box)
        self.properties.show_style("Helvetica", 11.0, False, False, "#000000", "")
        self._style_overrides = {}
        # the overlay floats over the rendered page: display-space position
        disp = self.view.rect_to_display(page, Rect(*box))
        frame = self.view.frames[page]
        overlay = TextOverlay(
            self.view.viewport(), self.theme,
            frame.x() + 20, frame.y()
            + disp.y0 * frame.zoom
            - self.view.verticalScrollBar().value(),
            280, "", "Helvetica", 11.0 * self.view.zoom, multiline=True)
        overlay.applied.connect(self._submit_insert)
        overlay.cancelled.connect(self._on_overlay_cancelled)
        self._overlay = overlay
        overlay.show()

    def _submit_insert(self, text: str, auto_shrink: bool = False) -> None:
        ctx = self._insert_ctx
        if ctx is None or self.controller is None or not text.strip():
            self._insert_ctx = None
            if self.view:
                self.view.set_layout_box(None, None)
            return
        if self._overlay is not None:
            self._overlay = None
        ov = self._style_overrides
        edit = ReplacementEdit(
            region_id=f"insert:p{ctx['page']}:r{self.controller.session.revision}",
            source_revision=self.controller.session.revision,
            new_text=text,
            mode=EditMode.REFLOW_BOX,
            target_box=Rect(*ctx["box"]),
            font_override=ov.get("font"),
            size_override=ov.get("size"),
            color_override=ov.get("color"),
            bold_override=ov.get("bold"),
            italic_override=ov.get("italic"),
            alignment=_ALIGN_FROM_STR.get(ov.get("align", "left"), TextAlignment.LEFT),
            auto_shrink=auto_shrink,
            page_index=ctx["page"],
        )
        self._pending_edit = {"page": ctx["page"], "region": None,
                              "box": ctx["box"], "text": text}
        self.statusBar().showMessage("Preparing new text…", 3000)
        self.controller.prepare_insert(ctx["page"], ctx["box"], edit)

    # -- insert-specific result handling -----------------------------------
    def _on_insert_prepared(self, info: dict) -> None:
        """Overflow/cancel handling for Add Text; success goes through the
        shared `_on_edit_prepared` confirm path."""
        pending = self._pending_edit
        if pending is None or pending.get("region") is not None:
            return  # not an insert
        if info["ok"]:
            return  # shared path shows the preview
        validation = info.get("validation")
        if validation is not None and validation.overflowed:
            choice = self._ask_overflow(validation, can_enlarge_box=False)
            if choice == "shrink":
                self._submit_insert(pending["text"], auto_shrink=True)
                return
            if choice == "shorten":
                self._on_add_text_requested(pending["page"], *pending["box"][:2])
                # pre-fill with the attempted text
                if self._overlay is not None:
                    self._overlay.editor.setPlainText(pending["text"])
                return
            self._pending_edit = None
            self._insert_ctx = None
            if self.view:
                self.view.set_layout_box(None, None)
            self.statusBar().showMessage("Add Text canceled.", 4000)
            return
        # other failures fall through to the shared warning path
        self._on_edit_prepared(info)

    def _clear_selection_state(self) -> None:
        self._selected_region = None
        self._pending_edit = None
        self._layout_box = None
        self._insert_ctx = None
        self._close_overlay()
        self.properties.clear_style()
        if self.view:
            self.view.set_selected_region(None, None)
            self.view.set_layout_box(None, None)

    def _zoom_in(self) -> None:
        if self.view:
            self.view.set_zoom(self.view.zoom * 1.25)

    def _zoom_out(self) -> None:
        if self.view:
            self.view.set_zoom(self.view.zoom / 1.25)

    def _fit_width(self) -> None:
        if self.view:
            self.view.fit_width()

    def _fit_page(self) -> None:
        if self.view:
            self.view.fit_page()

    def _goto_page(self, page: int) -> None:
        if self.view:
            self.view.scroll_to_page(page)

    # -- search ---------------------------------------------------------------
    def _focus_search(self) -> None:
        self.sidebar.show()
        self.action_sidebar.setChecked(True)
        self.sidebar_tabs.setCurrentWidget(self.sidebar_tabs.search)
        if self.sidebar_tabs.search.field.isEnabled():
            self.sidebar_tabs.search.field.setFocus()
            self.sidebar_tabs.search.field.selectAll()

    def _on_search_requested(self, query: str) -> None:
        if self.controller and self.controller.session:
            self.controller.search(query, self.sidebar_tabs.search.match_case)
            self.statusBar().showMessage(f"Searching for “{query}”…", 3000)

    def _on_search_done(self, results: list, truncated: bool) -> None:
        items = [(r["page"], r["snippet"], r.get("rect")) for r in results]
        self.sidebar_tabs.search.show_results(items)
        highlights: dict[int, list] = {}
        for r in results:
            if r.get("rect"):
                highlights.setdefault(r["page"], []).append(tuple(r["rect"]))
        if self.view:
            self.view.set_search_highlights(highlights)
        if truncated:
            self.statusBar().showMessage(
                f"More than {len(results)} matches — showing the first {len(results)}.", 6000)

    def _on_search_result(self, page: int, rect) -> None:
        if self.view:
            self.view.scroll_to_page(page)

    # -- document info / about ----------------------------------------------------
    def _show_doc_info(self) -> None:
        if not self._has_doc():
            return
        s = self.controller.session
        meta = s.meta
        box = QMessageBox(self)
        box.setWindowTitle("Document Info")
        box.setIcon(QMessageBox.Information)
        box.setText(f"<b>{s.display_name}</b>")
        rows = [
            f"Path: {s.path}",
            f"Pages: {meta.page_count}",
            f"Encrypted: {'yes' if meta.is_encrypted else 'no'}",
            f"Digitally signed: {'yes' if meta.is_signed else 'no'}",
            f"Title: {meta.title or '—'}",
            f"Source fingerprint: {s.fingerprint[:24]}…",
        ]
        box.setInformativeText("\n".join(rows))
        box.exec()

    def _show_about(self) -> None:
        QMessageBox.about(
            self, "About openPDF suite",
            f"<h3>openPDF suite {__version__}</h3>"
            "<p>Professional local-first PDF text editor for Windows.</p>"
            "<p>Built with Python, PySide6 (Qt), and PyMuPDF. Your documents never "
            "leave this computer.</p>"
            "<p>PyMuPDF is used under the AGPL-3.0 — see THIRD_PARTY_NOTICES.md.</p>",
        )

    def _show_limitations(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Editing Limitations")
        box.setIcon(QMessageBox.Information)
        box.setText(
            "openPDF suite edits text reliably in most digitally-created PDFs, with honest limits:"
        )
        box.setInformativeText(
            "• Scanned pages are viewable but not editable as text (OCR is future work).\n"
            "• Text converted to vector outlines is not ordinary editable text.\n"
            "• Rotated, skewed, curved, or heavily transformed text may be read-only.\n"
            "• Exact original-font fidelity isn't promised; substitutions are always shown.\n"
            "• Pages with existing redaction annotations are read-only for safety.\n"
            "• Black Out really removes marked text — but on scanned pages it only "
            "covers the image; the scan still contains the content.\n"
            "• Editing invalidates digital signatures (unavoidable in any PDF editor).\n"
            "• No automatic reflow across pages; paragraphs reflow inside their box only."
        )
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()

    # -- misc slots ----------------------------------------------------------------
    def _toggle_sidebar(self, checked: bool) -> None:
        self.sidebar.setVisible(checked)

    def _toggle_properties(self, checked: bool) -> None:
        self.props_dock.setVisible(checked)

    def _toggle_reduced_motion(self, checked: bool) -> None:
        self.settings.reduced_motion = checked

    def _on_close_doc(self) -> None:
        self._teardown_controller()
        self._current_path = None
        self.stack.setCurrentIndex(0)
        self.welcome.set_recents(self.settings.recent_files())
        self.page_label.setText("No document")
        self.zoom_label.setText(" 100% ")
        self.zoom_status.setText("")
        self._update_action_availability()
        self._refresh_title()

    # -- drag & drop ----------------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".pdf"):
                self.open_document(path)
                event.acceptProposedAction()
                return

    # -- window lifecycle ----------------------------------------------------------
    def closeEvent(self, event) -> None:
        self._flush_recovery_on_close()
        self.settings.save_geometry("main", self.saveGeometry())
        self.settings.save_state("main", self.saveState())
        self.settings.sync()
        if self.controller is not None:
            self.controller.shutdown()
            self.controller = None
        super().closeEvent(event)

    def _flush_recovery_on_close(self) -> None:
        """Persist the edited state synchronously so closing never loses work."""
        sess = self.controller.session if self.controller else None
        if sess is None or not sess.dirty:
            return
        loop = QEventLoop(self)
        got: dict = {}

        def on_snap(doc_id: str, revision: int, data: bytes, path: str) -> None:
            if self.controller and self.controller.session                     and doc_id == self.controller.session.doc_id:
                got["snap"] = (doc_id, data)
                loop.quit()

        self.controller.snapshot_ready.connect(on_snap)
        QTimer.singleShot(3000, loop.quit)  # never block close for long
        self.controller.snapshot()
        loop.exec()
        try:
            self.controller.snapshot_ready.disconnect(on_snap)
        except RuntimeError:
            pass
        if "snap" in got:
            doc_id, data = got["snap"]
            key = self._recovery_key or doc_id
            self.recovery.write(key, str(sess.path or ""), sess.fingerprint,
                                sess.revision, sess.saved_revision, data)
