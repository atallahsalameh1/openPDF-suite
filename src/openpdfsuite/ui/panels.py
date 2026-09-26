"""Left sidebar (thumbnails + search) and right properties panel.

M1 scope: fully built, styled, and honest — panels show their empty states and
disabled controls with explanations until the viewer (M2) and editing (M3)
wire them up.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .themes import ThemeManager


class ThumbnailPanel(QWidget):
    """Page thumbnails; clicking one requests navigation (wired in M2)."""

    page_requested = Signal(int)

    def __init__(self, theme: ThemeManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.list = QListWidget()
        self.list.setObjectName("ThumbnailList")
        self.list.setIconSize(QSize(96, 124))
        self.list.setSpacing(8)
        self.list.setUniformItemSizes(False)
        self.list.itemClicked.connect(self._on_click)
        layout.addWidget(self.list)

        self.empty = QLabel("No document open.")
        self.empty.setProperty("role", "caption")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

    def _on_click(self, item: QListWidgetItem) -> None:
        page = item.data(Qt.UserRole)
        if page is not None:
            self.page_requested.emit(int(page))

    def clear_pages(self) -> None:
        self.list.clear()
        self.empty.show()

    def set_page_count(self, count: int) -> None:
        self.list.clear()
        self.empty.hide()
        for i in range(count):
            item = QListWidgetItem(f"Page {i + 1}")
            item.setData(Qt.UserRole, i)
            item.setTextAlignment(Qt.AlignCenter)
            self.list.addItem(item)

    def set_thumbnail(self, page_index: int, pixmap: QPixmap) -> None:
        if 0 <= page_index < self.list.count():
            self.list.item(page_index).setIcon(pixmap)

    def select_page(self, page_index: int) -> None:
        if 0 <= page_index < self.list.count():
            self.list.setCurrentRow(page_index)


class SearchPanel(QWidget):
    """Document search with result list (wired to the worker in M2)."""

    search_requested = Signal(str)
    result_activated = Signal(int, object)  # page index, rect payload

    def __init__(self, theme: ThemeManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        row = QHBoxLayout()
        self.field = QLineEdit()
        self.field.setPlaceholderText("Search in document…")
        self.field.setClearButtonEnabled(True)
        self.field.returnPressed.connect(self._submit)
        row.addWidget(self.field)
        self.button = QPushButton()
        self.button.setIcon(theme.icon("search", 16))
        self.button.setFixedWidth(34)
        self.button.setToolTip("Search")
        self.button.clicked.connect(self._submit)
        row.addWidget(self.button)
        layout.addLayout(row)

        self.options = QCheckBox("Match case")
        layout.addWidget(self.options)
        # search is wired to the worker in M2; until then disabled with explanation
        self.set_search_enabled(False)

        self.status = QLabel("")
        self.status.setProperty("role", "caption")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.results = QListWidget()
        self.results.setObjectName("SearchResults")
        self.results.itemActivated.connect(self._on_result)
        layout.addWidget(self.results)

        self.blackout_btn = QPushButton("Black out all…")
        self.blackout_btn.setToolTip(
            "Turn every search hit into a reviewable black-out mark")
        self.blackout_btn.clicked.connect(self._on_blackout_all)
        self.blackout_btn.hide()
        layout.addWidget(self.blackout_btn)

        self.empty = QLabel("No results yet.\nType a query and press Enter.")
        self.empty.setProperty("role", "caption")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        self._last_items: list[tuple[int, str, object]] = []

    def set_blackout_visible(self, visible: bool) -> None:
        """'Black out all…' is only meaningful in Black Out mode (M9)."""
        self.blackout_btn.setVisible(visible and bool(self._last_items))

    def _on_blackout_all(self) -> None:
        hits = [(page, rect) for page, _snippet, rect in self._last_items
                if rect is not None]
        if hits:
            self.blackout_all_requested.emit(hits)

    blackout_all_requested = Signal(list)  # [(page, rect), ...] engine space

    def set_search_enabled(self, enabled: bool) -> None:
        hint = "" if enabled else "Open a document to search it."
        for w in (self.field, self.button, self.options):
            w.setEnabled(enabled)
            w.setToolTip(hint)

    def _submit(self) -> None:
        query = self.field.text().strip()
        if query:
            self.search_requested.emit(query)

    def _on_result(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.UserRole)
        if data:
            page, rect = data
            self.result_activated.emit(int(page), rect)

    @property
    def match_case(self) -> bool:
        return self.options.isChecked()

    def show_results(self, items: list[tuple[int, str, object]]) -> None:
        """items: (page_index, snippet, rect_payload)."""
        self._last_items = list(items)
        self.results.clear()
        for page, snippet, rect in items:
            item = QListWidgetItem(f"p.{page + 1}  {snippet}")
            item.setData(Qt.UserRole, (page, rect))
            item.setToolTip(snippet)
            self.results.addItem(item)
        n = len(items)
        self.empty.setVisible(n == 0)
        self.empty.setText("No matches found." if n == 0 else self.empty.text())
        self.status.setText(f"{n} result" + ("" if n == 1 else "s"))
        self.blackout_btn.setVisible(bool(items))

    def clear_results(self) -> None:
        self.results.clear()
        self._last_items = []
        self.blackout_btn.hide()
        self.status.setText("")
        self.empty.setText("No results yet.\nType a query and press Enter.")
        self.empty.show()


class RedactPanel(QWidget):
    """Pending black-out marks (M9): reviewable list before applying."""

    mark_activated = Signal(int)  # page to scroll to
    mark_remove_requested = Signal(int, object)  # page, Rect
    clear_requested = Signal()

    def __init__(self, theme: ThemeManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.hint = QLabel(
            "Black Out mode: drag a box over text to mark it, or use Search → "
            "“Black out all…”. Apply Redaction removes the marked text "
            "permanently — it cannot be undone after saving.")
        self.hint.setProperty("role", "caption")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.marks = QListWidget()
        self.marks.setObjectName("RedactMarks")
        self.marks.itemActivated.connect(self._on_mark)
        layout.addWidget(self.marks)

        self.empty = QLabel("No black-out marks yet.")
        self.empty.setProperty("role", "caption")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        buttons = QHBoxLayout()
        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self._remove_selected)
        buttons.addWidget(self.remove_btn)
        self.clear_btn = QPushButton("Clear all")
        self.clear_btn.clicked.connect(self.clear_requested)
        buttons.addWidget(self.clear_btn)
        layout.addLayout(buttons)

    def _on_mark(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.UserRole)
        if data:
            self.mark_activated.emit(int(data[0]))

    def _remove_selected(self) -> None:
        item = self.marks.currentItem()
        if item is None:
            return
        page, rect = item.data(Qt.UserRole)
        self.mark_remove_requested.emit(int(page), rect)

    def show_marks(self, items: list[tuple[int, str, object]]) -> None:
        """items: (page_index, snippet, engine-space Rect)."""
        self.marks.clear()
        for page, snippet, rect in items:
            label = f"p.{page + 1}  {snippet}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, (page, rect))
            item.setToolTip(label)
            self.marks.addItem(item)
        n = len(items)
        self.empty.setVisible(n == 0)
        self.remove_btn.setEnabled(n > 0)
        self.clear_btn.setEnabled(n > 0)


class PropertiesPanel(QWidget):
    """Context-sensitive right panel (content wired in M3)."""

    font_change_requested = Signal(str)
    size_change_requested = Signal(float)
    color_change_requested = Signal(object)
    alignment_change_requested = Signal(str)
    bold_toggled = Signal(bool)
    italic_toggled = Signal(bool)

    def __init__(self, theme: ThemeManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.setObjectName("PropertiesPanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(12)

        heading = QLabel("Properties")
        heading.setProperty("role", "subheading")
        outer.addWidget(heading)

        self.hint = QLabel(
            "Select a text region in Edit Text mode to see and change its style."
        )
        self.hint.setProperty("role", "caption")
        self.hint.setWordWrap(True)
        outer.addWidget(self.hint)

        card = QFrame()
        card.setObjectName("StyleCard")
        form = QFormLayout(card)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft)

        self.font_combo = QComboBox()
        self.font_combo.setEditable(True)
        self.font_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.font_combo.currentTextChanged.connect(self.font_change_requested)
        form.addRow("Font", self.font_combo)

        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(4.0, 400.0)
        self.size_spin.setDecimals(1)
        self.size_spin.setSuffix(" pt")
        self.size_spin.valueChanged.connect(
            lambda v: self.size_change_requested.emit(float(v))
        )
        form.addRow("Size", self.size_spin)

        style_row = QHBoxLayout()
        self.bold_btn = QPushButton("B")
        self.bold_btn.setCheckable(True)
        self.bold_btn.setFixedWidth(36)
        f = self.bold_btn.font()
        f.setBold(True)
        self.bold_btn.setFont(f)
        self.bold_btn.toggled.connect(self.bold_toggled)
        style_row.addWidget(self.bold_btn)
        self.italic_btn = QPushButton("I")
        self.italic_btn.setCheckable(True)
        self.italic_btn.setFixedWidth(36)
        f = self.italic_btn.font()
        f.setItalic(True)
        self.italic_btn.setFont(f)
        self.italic_btn.toggled.connect(self.italic_toggled)
        style_row.addWidget(self.italic_btn)
        style_row.addStretch(1)
        form.addRow("Style", self._wrap(style_row))

        self.color_btn = QPushButton("Color…")
        self.color_btn.clicked.connect(self._pick_color)
        form.addRow("Color", self.color_btn)

        self.align_combo = QComboBox()
        self.align_combo.addItems(["Left", "Center", "Right", "Justify"])
        self.align_combo.currentTextChanged.connect(
            lambda s: self.alignment_change_requested.emit(s.lower())
        )
        form.addRow("Align", self.align_combo)

        outer.addWidget(card)

        self.substitution = QLabel("")
        self.substitution.setProperty("role", "warning")
        self.substitution.setWordWrap(True)
        self.substitution.hide()
        outer.addWidget(self.substitution)

        outer.addStretch(1)
        self.set_enabled(False)

    def _wrap(self, layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _pick_color(self) -> None:
        from PySide6.QtWidgets import QColorDialog

        c = QColorDialog.getColor(parent=self)
        if c.isValid():
            self.color_change_requested.emit(c)

    def set_enabled(self, enabled: bool) -> None:
        for w in (self.font_combo, self.size_spin, self.bold_btn, self.italic_btn,
                  self.color_btn, self.align_combo):
            w.setEnabled(enabled)

    def show_style(self, family: str, size: float, bold: bool, italic: bool,
                   color_hex: str, substitution_note: str = "") -> None:
        self.set_enabled(True)
        self.font_combo.blockSignals(True)
        self.font_combo.setCurrentText(family)
        self.font_combo.blockSignals(False)
        self.size_spin.blockSignals(True)
        self.size_spin.setValue(size)
        self.size_spin.blockSignals(False)
        self.bold_btn.blockSignals(True)
        self.bold_btn.setChecked(bold)
        self.bold_btn.blockSignals(False)
        self.italic_btn.blockSignals(True)
        self.italic_btn.setChecked(italic)
        self.italic_btn.blockSignals(False)
        self.color_btn.setStyleSheet(
            f"QPushButton {{ border-left: 14px solid {color_hex}; }}"
        )
        self.substitution.setText(substitution_note)
        self.substitution.setVisible(bool(substitution_note))

    def clear_style(self) -> None:
        self.set_enabled(False)
        self.substitution.hide()
        self.color_btn.setStyleSheet("")


class SidebarTabs(QTabWidget):
    """Left sidebar container: Pages + Search + Black Out."""

    def __init__(self, theme: ThemeManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("SidebarPanel")
        self.thumbnails = ThumbnailPanel(theme)
        self.search = SearchPanel(theme)
        self.redact = RedactPanel(theme)
        self.addTab(self.thumbnails, "Pages")
        self.addTab(self.search, "Search")
        self.addTab(self.redact, "Black Out")
