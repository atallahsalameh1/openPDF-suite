"""Welcome screen: Open PDF + recent documents + drag-drop hint (AGENTS.md §4)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .components.icons import logo_pixmap
from .themes import ThemeManager


class WelcomeScreen(QWidget):
    """Shown when no document is open."""

    open_requested = Signal()
    recent_activated = Signal(str)  # path

    def __init__(self, theme: ThemeManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme = theme
        self.setObjectName("WelcomeScreen")
        self._build()
        theme.theme_changed.connect(self._retheme)
        self._retheme()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 32, 48, 32)
        outer.addStretch(1)

        card = QFrame()
        card.setObjectName("WelcomeCard")
        card.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(48, 40, 48, 40)
        layout.setSpacing(16)

        logo = QLabel()
        logo.setObjectName("WelcomeLogo")
        logo.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo)
        self._logo_label = logo

        title = QLabel("openPDF suite")
        title.setProperty("role", "heading")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Professional PDF text editing, locally and privately.")
        subtitle.setProperty("role", "secondary")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        layout.addSpacing(12)

        open_btn = QPushButton("Open PDF…")
        open_btn.setProperty("accent", True)
        open_btn.setProperty("big", True)
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.clicked.connect(self.open_requested)
        open_btn.setObjectName("WelcomeOpenButton")
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(open_btn)
        row.addStretch(1)
        layout.addLayout(row)

        drop_hint = QLabel("or drop a PDF anywhere in this window")
        drop_hint.setProperty("role", "caption")
        drop_hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(drop_hint)

        layout.addSpacing(20)

        recent_head = QLabel("Recent documents")
        recent_head.setProperty("role", "subheading")
        layout.addWidget(recent_head)

        self.recents = QListWidget()
        self.recents.setObjectName("WelcomeRecents")
        self.recents.setFixedWidth(460)
        self.recents.setMaximumHeight(220)
        self.recents.itemDoubleClicked.connect(self._on_recent)
        self.recents.itemClicked.connect(self._on_recent)
        layout.addWidget(self.recents, 0, Qt.AlignHCenter)

        self.empty_recents = QLabel("No recent documents yet.")
        self.empty_recents.setProperty("role", "caption")
        self.empty_recents.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.empty_recents)
        self.empty_recents.hide()

        outer.addWidget(card, 0, Qt.AlignHCenter | Qt.AlignVCenter)
        outer.addStretch(2)

    def _retheme(self, *_args) -> None:
        logo = logo_pixmap(56)
        if logo is not None:
            self._logo_label.setPixmap(logo)
        else:  # brand asset missing — fall back to the vector document mark
            self._logo_label.setPixmap(
                self.theme.icon("logo", 56, self.theme.accent_color).pixmap(56, 56)
            )
        # recents icons
        for i in range(self.recents.count()):
            item = self.recents.item(i)
            item.setIcon(self.theme.icon("clock", 16, self.theme.tokens.text_secondary))

    def set_recents(self, paths: list[str]) -> None:
        self.recents.clear()
        for p in paths:
            item = QListWidgetItem(str(Path(p).name))
            item.setToolTip(p)
            item.setData(Qt.UserRole, p)
            item.setIcon(self.theme.icon("clock", 16, self.theme.tokens.text_secondary))
            if not Path(p).exists():
                item.setText(f"{Path(p).name}  (missing)")
                item.setFlags(Qt.NoItemFlags)
            self.recents.addItem(item)
        has = self.recents.count() > 0
        self.recents.setVisible(has)
        self.empty_recents.setVisible(not has)

    def _on_recent(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.UserRole)
        if path and Path(path).exists():
            self.recent_activated.emit(path)
