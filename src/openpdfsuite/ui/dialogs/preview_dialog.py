"""Authoritative preview dialog (AGENTS.md §5: Preview) + overflow choices (§9).

Shows the PDF-engine-rendered BEFORE and AFTER of the edited page, cropped
around the changed region, plus honest notices: font substitution, auto-shrink
results, and any validation warnings. Nothing here is Qt-laid-out text — both
images come from PyMuPDF renders of the real candidate document.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...domain.models import Rect
from ..themes import ThemeManager


def _crop(png: bytes, box: Rect, preview_zoom: float, pad: float = 8.0) -> QPixmap:
    from PySide6.QtCore import QRect

    pix = QPixmap()
    pix.loadFromData(png)
    if pix.isNull():
        return pix
    x0 = max(0, int(box.x0 * preview_zoom) - int(pad))
    y0 = max(0, int(box.y0 * preview_zoom) - int(pad))
    x1 = min(pix.width(), int(box.x1 * preview_zoom) + int(pad))
    y1 = min(pix.height(), int(box.y1 * preview_zoom) + int(pad))
    w, h = max(8, x1 - x0), max(8, y1 - y0)
    return pix.copy(QRect(x0, y0, w, h))


class PreviewDialog(QDialog):
    """Before/after comparison. OK applies the edit; Cancel discards it."""

    def __init__(self, parent: QWidget, theme: ThemeManager,
                 before_png: bytes, after_png: bytes, changed_box: Rect,
                 preview_zoom: float, validation, issues: list[str],
                 apply_label: str = "Apply edit",
                 title: str = "Preview replacement"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # notices
        notices: list[tuple[str, str]] = []  # (kind, text)
        if validation is not None and getattr(validation, "substituted_font", None):
            notices.append(("warning",
                            f"Original font unavailable or unusable. Using "
                            f"{validation.substituted_font}."))
        for issue in issues:
            if "auto-shrunk" in issue:
                notices.append(("warning", issue))
            elif issue:
                notices.append(("info", issue))
        for kind, text in notices:
            frame = QFrame()
            frame.setProperty("notice", kind)
            fl = QHBoxLayout(frame)
            fl.setContentsMargins(10, 8, 10, 8)
            icon = QLabel()
            icon.setPixmap(theme.icon("warning" if kind == "warning" else "info",
                                      16, theme.tokens.warning if kind == "warning"
                                      else theme.tokens.accent).pixmap(16, 16))
            fl.addWidget(icon)
            label = QLabel(text)
            label.setWordWrap(True)
            fl.addWidget(label, 1)
            layout.addWidget(frame)

        images = QHBoxLayout()
        images.setSpacing(12)
        for title, png in (("Before", before_png), ("After (PDF engine)", after_png)):
            col = QVBoxLayout()
            head = QLabel(title)
            head.setProperty("role", "subheading")
            head.setAlignment(Qt.AlignCenter)
            col.addWidget(head)
            pic = QLabel()
            pic.setAlignment(Qt.AlignCenter)
            cropped = _crop(png, changed_box, preview_zoom)
            if not cropped.isNull():
                if cropped.width() > 340:
                    cropped = cropped.scaledToWidth(
                        340, Qt.SmoothTransformation)
                pic.setPixmap(cropped)
            else:
                pic.setText("(render unavailable)")
            pic.setStyleSheet(
                f"border: 1px solid {theme.tokens.border}; border-radius: 8px;"
                f"background: white; padding: 4px;")
            col.addWidget(pic)
            images.addLayout(col)
        layout.addLayout(images)

        note = QLabel("Both images are rendered by the PDF engine that writes "
                      "your file — what you see is what will be saved.")
        note.setProperty("role", "caption")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(apply_label)
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class OverflowDialog(QDialog):
    """Explicit overflow choices (§9): reduce font / shorten text / cancel.

    `choice` is one of "shrink", "shorten", "cancel".
    """

    def __init__(self, parent: QWidget, theme: ThemeManager,
                 suggested_size: float | None, overflow_pt: float,
                 can_enlarge_box: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Text does not fit")
        self.choice = "cancel"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        frame = QFrame()
        frame.setProperty("notice", "warning")
        fl = QHBoxLayout(frame)
        fl.setContentsMargins(10, 8, 10, 8)
        icon = QLabel()
        icon.setPixmap(theme.icon("warning", 16, theme.tokens.warning).pixmap(16, 16))
        fl.addWidget(icon)
        msg = QLabel(
            f"The replacement needs about {overflow_pt:.0f} pt more space than "
            "available. openPDF suite never silently shrinks text or lets it overlap "
            "neighbors. Choose how to continue:")
        msg.setWordWrap(True)
        fl.addWidget(msg, 1)
        layout.addWidget(frame)

        if can_enlarge_box:
            b = QPushButton("Enlarge the text box")
            b.clicked.connect(lambda: self._done("enlarge"))
            layout.addWidget(b)
        if suggested_size and suggested_size >= 4.0:
            b = QPushButton(f"Reduce font size to {suggested_size:g} pt")
            b.clicked.connect(lambda: self._done("shrink"))
            layout.addWidget(b)
        b = QPushButton("Shorten the text (back to editing)")
        b.clicked.connect(lambda: self._done("shorten"))
        layout.addWidget(b)
        b = QPushButton("Cancel")
        b.clicked.connect(lambda: self._done("cancel"))
        layout.addWidget(b)

    def _done(self, choice: str) -> None:
        self.choice = choice
        self.accept() if choice != "cancel" else self.reject()
