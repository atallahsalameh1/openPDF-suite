"""Find & Replace All dialog (M10, AGENTS.md §3/§5/§9).

Stage 1 collects the find/replace text and options; "Review Changes…" plans
the whole document in the worker. Stage 2 is the review window: every match
as a checkbox (skip anything you don't want), a before/after render of the
selected match's page from the PDF engine, and an honest per-match skip
reason. Apply commits the checked matches as one undoable group.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...domain.models import Rect
from ..themes import ThemeManager
from .preview_dialog import _crop


class ReplaceDialog(QDialog):
    """Two-stage Replace All: options -> review window -> one grouped apply."""

    apply_requested = Signal(str, int)  # query, number of checked matches

    def __init__(self, parent: QWidget, theme: ThemeManager, controller):
        super().__init__(parent)
        self.theme = theme
        self.controller = controller
        self._plan: dict | None = None
        self._batch_key: str | None = None
        self._page_previews: dict[int, dict] = {}

        self.setWindowTitle("Replace All")
        self.setMinimumSize(860, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # -- stage 1: options -------------------------------------------------
        self.options_page = QWidget(self)
        opt = QVBoxLayout(self.options_page)
        opt.setContentsMargins(0, 0, 0, 0)
        opt.setSpacing(8)

        find_row = QHBoxLayout()
        find_label = QLabel("Find what:")
        find_label.setMinimumWidth(90)
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Text to find")
        self.find_edit.textChanged.connect(self._update_review_enabled)
        find_row.addWidget(find_label)
        find_row.addWidget(self.find_edit, 1)
        opt.addLayout(find_row)

        repl_row = QHBoxLayout()
        repl_label = QLabel("Replace with:")
        repl_label.setMinimumWidth(90)
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("Replacement text (leave empty to delete the text)")
        repl_row.addWidget(repl_label)
        repl_row.addWidget(self.replace_edit, 1)
        opt.addLayout(repl_row)

        checks = QHBoxLayout()
        self.cb_case = QCheckBox("Match case")
        self.cb_word = QCheckBox("Whole words only")
        self.cb_shrink = QCheckBox("Shrink to fit when text doesn't fit")
        checks.addWidget(self.cb_case)
        checks.addWidget(self.cb_word)
        checks.addWidget(self.cb_shrink)
        checks.addStretch(1)
        opt.addLayout(checks)

        hint = QLabel("Whole lines are rebuilt in place with the original font, "
                      "size and color. Matches are found per line — text that "
                      "spans a line break is never found.")
        hint.setProperty("role", "caption")
        hint.setWordWrap(True)
        opt.addWidget(hint)
        layout.addWidget(self.options_page)

        # -- stage 2: review window -------------------------------------------
        self.review_page = QWidget(self)
        rev = QVBoxLayout(self.review_page)
        rev.setContentsMargins(0, 0, 0, 0)
        rev.setSpacing(8)

        self.summary = QLabel()
        self.summary.setProperty("role", "subheading")
        rev.addWidget(self.summary)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.match_list = QListWidget()
        self.match_list.setObjectName("ReplaceMatches")
        self.match_list.itemChanged.connect(self._update_apply_button)
        self.match_list.currentItemChanged.connect(self._show_preview)
        body.addWidget(self.match_list, 5)

        preview_col = QVBoxLayout()
        preview_col.setSpacing(4)
        self.preview_page_label = QLabel(" ")
        self.preview_page_label.setAlignment(Qt.AlignCenter)
        self.preview_page_label.setProperty("role", "subheading")
        preview_col.addWidget(self.preview_page_label)
        pics = QHBoxLayout()
        pics.setSpacing(10)
        self.before_pic = self._make_pic()
        self.after_pic = self._make_pic()
        before_col = QVBoxLayout()
        before_head = QLabel("Before")
        before_head.setAlignment(Qt.AlignCenter)
        before_head.setProperty("role", "caption")
        before_col.addWidget(before_head)
        before_col.addWidget(self.before_pic)
        after_col = QVBoxLayout()
        after_head = QLabel("After (PDF engine)")
        after_head.setAlignment(Qt.AlignCenter)
        after_head.setProperty("role", "caption")
        after_col.addWidget(after_head)
        after_col.addWidget(self.after_pic)
        pics.addLayout(before_col, 1)
        pics.addLayout(after_col, 1)
        preview_col.addLayout(pics, 1)
        preview_note = QLabel("Rendered by the PDF engine that writes your file.")
        preview_note.setProperty("role", "caption")
        preview_note.setAlignment(Qt.AlignCenter)
        preview_col.addWidget(preview_note)
        body.addLayout(preview_col, 6)
        rev.addLayout(body, 1)
        layout.addWidget(self.review_page)
        self.review_page.setVisible(False)

        # -- footer -------------------------------------------------------------
        self.status = QLabel(" ")
        self.status.setProperty("role", "caption")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.review_btn = QPushButton("Review Changes…")
        self.review_btn.setProperty("accent", True)
        self.review_btn.setEnabled(False)
        self.review_btn.clicked.connect(self._on_review)
        self.apply_btn = QPushButton("Apply All")
        self.apply_btn.setProperty("accent", True)
        self.apply_btn.setEnabled(False)
        self.apply_btn.setVisible(False)
        self.apply_btn.clicked.connect(self._on_apply)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(self.review_btn)
        footer.addWidget(self.apply_btn)
        footer.addWidget(cancel_btn)
        layout.addLayout(footer)

        self.controller.replace_planned.connect(self._on_planned)

    # -- helpers --------------------------------------------------------------
    def _make_pic(self) -> QLabel:
        pic = QLabel()
        pic.setAlignment(Qt.AlignCenter)
        pic.setMinimumSize(260, 180)
        pic.setStyleSheet(
            f"border: 1px solid {self.theme.tokens.border}; border-radius: 8px;"
            f"background: white; padding: 4px;")
        return pic

    def _update_review_enabled(self) -> None:
        self.review_btn.setEnabled(bool(self.find_edit.text().strip()))

    # -- plan -------------------------------------------------------------------
    def _on_review(self) -> None:
        self.status.setText("Finding matches…")
        self.review_btn.setEnabled(False)
        self.controller.plan_replace_all(
            self.find_edit.text(), self.replace_edit.text(),
            match_case=self.cb_case.isChecked(),
            whole_word=self.cb_word.isChecked(),
            auto_shrink=self.cb_shrink.isChecked())

    def _on_planned(self, info: dict) -> None:
        self.review_btn.setEnabled(bool(self.find_edit.text().strip()))
        if not info.get("ok"):
            self.status.setText(f"Replace All failed: {info.get('error') or 'unknown error'}")
            return
        self._plan = info
        self._batch_key = info.get("batch_key")
        self._page_previews = {int(pg["page"]): pg for pg in info.get("pages", [])}
        matches = list(info.get("matches", []))
        self.match_list.blockSignals(True)
        self.match_list.clear()
        for m in matches:
            page = int(m["page"]) + 1
            planned = m["status"] == "planned"
            item = QListWidgetItem(f"page {page} — {m['snippet']}")
            item.setData(Qt.UserRole, int(m["id"]))
            if planned:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
                note = m.get("reason") or ""
                if note:
                    item.setToolTip(note)
            else:
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
                item.setForeground(QColor(self.theme.tokens.text_secondary))
                item.setToolTip(f"Skipped: {m.get('reason') or 'cannot be replaced'}")
                item.setText(item.text() + "   (skipped)")
            self.match_list.addItem(item)
        self.match_list.blockSignals(False)

        total = int(info.get("total", 0))
        ready = int(info.get("ready", 0))
        skipped = int(info.get("skipped", 0))
        n_pages = len(self._page_previews)
        if total == 0:
            self.summary.setText("No matches found in this document.")
        else:
            parts = [f"{ready} of {total} matches will change on {n_pages} page(s)"]
            if skipped:
                parts.append(f"{skipped} skipped (see the list)")
            self.summary.setText(" · ".join(parts) + ".")
        if info.get("truncated"):
            note = QLabel(f"Only the first {total} matches are shown "
                          f"(search limit). Run Replace All again after saving "
                          f"to handle more.")
            note.setProperty("role", "caption")
            note.setWordWrap(True)
            self.review_page.layout().insertWidget(1, note)
        self.options_page.setVisible(False)
        self.review_page.setVisible(True)
        self.review_btn.setVisible(False)
        self.apply_btn.setVisible(True)
        self.apply_btn.setEnabled(ready > 0)
        self.status.setText("Uncheck anything you don't want to change, then "
                            "apply. One Ctrl+Z undoes the whole batch.")
        if matches:
            self.match_list.setCurrentRow(0)
        self._update_apply_button()

    # -- review -----------------------------------------------------------------
    def _checked_ids(self) -> list[int]:
        ids = []
        for i in range(self.match_list.count()):
            item = self.match_list.item(i)
            if item.flags() & Qt.ItemIsUserCheckable and item.checkState() == Qt.Checked:
                ids.append(int(item.data(Qt.UserRole)))
        return ids

    def _update_apply_button(self, *_args) -> None:
        if self._plan is not None:
            self.apply_btn.setText(f"Apply {len(self._checked_ids())} match(es)")

    def _show_preview(self, current: QListWidgetItem | None,
                      _previous: QListWidgetItem | None = None) -> None:
        for pic in (self.before_pic, self.after_pic):
            pic.setPixmap(QPixmap())
        if current is None or self._plan is None:
            self.preview_page_label.setText(" ")
            return
        mid = int(current.data(Qt.UserRole))
        match = next((m for m in self._plan.get("matches", [])
                      if int(m["id"]) == mid), None)
        if match is None:
            return
        if match["status"] != "planned":
            # skipped matches must say why in plain sight, not only in a tooltip
            self.status.setText(
                f"This match was skipped: {match.get('reason') or 'cannot be replaced.'}")
        else:
            self.status.setText("Uncheck anything you don't want to change, then "
                                "apply. One Ctrl+Z undoes the whole batch.")
        page = int(match["page"])
        self.preview_page_label.setText(f"Page {page + 1}")
        info = self._page_previews.get(page)
        if info is None:
            self.before_pic.setText("(no preview)")
            self.after_pic.setText("(no preview)")
            return
        rect = info.get("change_rect")
        box = Rect(*rect) if rect else None
        zoom = float(info.get("preview_zoom", 2.0))
        for pic, key in ((self.before_pic, "before_png"), (self.after_pic, "after_png")):
            png = info.get(key)
            if not png:
                pic.setText("(preview not available — many pages changed)")
                continue
            cropped = (_crop(png, box, zoom) if box
                       else self._full(png))
            if not cropped.isNull():
                if cropped.width() > 360:
                    cropped = cropped.scaledToWidth(360, Qt.SmoothTransformation)
                pic.setPixmap(cropped)

    @staticmethod
    def _full(png: bytes) -> QPixmap:
        pix = QPixmap()
        pix.loadFromData(png)
        return pix

    # -- apply --------------------------------------------------------------------
    def _on_apply(self) -> None:
        ids = self._checked_ids()
        if not ids or self._plan is None:
            return
        self.apply_requested.emit(self._plan.get("query", ""), len(ids))
        self.controller.apply_replace_all(self._batch_key, ids)
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        try:
            self.controller.replace_planned.disconnect(self._on_planned)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)
