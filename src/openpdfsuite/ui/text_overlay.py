"""In-place text editing overlay (AGENTS.md §5: Edit).

An editable overlay aligned to the selected PDF region. Qt's text layout is
only the input surface — the authoritative preview is rendered by the PDF
engine (PreviewDialog) before anything is applied.

Keys: Esc cancels, Ctrl+Enter applies, explicit Apply/Cancel buttons.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .components.motion import fade_in
from .themes import ThemeManager


class TextOverlay(QFrame):
    """Floating editor positioned over a region of a page frame."""

    applied = Signal(str)  # new text
    cancelled = Signal()

    def __init__(self, viewport: QWidget, theme: ThemeManager,
                 origin_x: float, origin_y: float, width_px: float,
                 text: str, font_family: str, font_size_px: float,
                 multiline: bool = False, parent=None):
        super().__init__(viewport)
        self.theme = theme
        self.setObjectName("TextOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            #TextOverlay {{
                background: {theme.tokens.bg_panel};
                border: 1.5px solid {theme.tokens.accent};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("OverlayEditor")
        self.editor.setPlainText(text)
        self.editor.setLineWrapMode(
            QPlainTextEdit.WidgetWidth if multiline else QPlainTextEdit.NoWrap)
        font = QFont(font_family)
        font.setPointSizeF(max(7.0, font_size_px * 0.78))  # px -> pt approx
        font.setBold(False)
        self.editor.setFont(font)
        self.editor.setStyleSheet(
            "QPlainTextEdit { background: white; color: #18202B; border: 1px solid "
            + theme.tokens.border + "; border-radius: 6px; padding: 4px; }")
        if not multiline:
            self.editor.setFixedHeight(int(font_size_px * 1.6) + 16)
        else:
            self.editor.setMinimumHeight(int(font_size_px * 4.6) + 16)
        layout.addWidget(self.editor)

        hint = QLabel("Ctrl+Enter applies · Esc cancels · preview is PDF-engine rendered")
        hint.setProperty("role", "caption")
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel)
        buttons.addWidget(self.cancel_btn)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setProperty("accent", True)
        self.apply_btn.setDefault(True)
        self.apply_btn.clicked.connect(self.apply)
        buttons.addWidget(self.apply_btn)
        layout.addLayout(buttons)

        w = max(int(width_px) + 24, 280)
        self.adjustSize()
        self.setFixedWidth(w)
        self.move(max(0, int(origin_x) - 12), max(0, int(origin_y) - 8))
        self.editor.setFocus()
        self.editor.selectAll()
        self.editor.installEventFilter(self)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 150 ms fade-in, suppressed when reduced motion is preferred (§4)
        fade_in(self, self.theme.settings)

    def eventFilter(self, obj, event):
        if obj is self.editor and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.cancel()
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) and (
                event.modifiers() & Qt.ControlModifier
            ):
                self.apply()
                return True
        return super().eventFilter(obj, event)

    def apply(self) -> None:
        text = self.editor.toPlainText()
        self.applied.emit(text)
        self.close()
        self.deleteLater()

    def cancel(self) -> None:
        self.cancelled.emit()
        self.close()
        self.deleteLater()
