"""Theme manager: resolves light/dark/system, applies QSS app-wide, notifies."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from ...infrastructure.settings import Settings
from ..components import icons
from .tokens import DARK, LIGHT, ThemeTokens, build_qss


def system_is_dark() -> bool:
    try:
        scheme = QApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except (AttributeError, RuntimeError):
        pass
    # fallback: luminance of the palette window color
    pal = QApplication.palette() if QApplication.instance() else QPalette()
    c = pal.color(QPalette.Window)
    return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255 < 0.5


class ThemeManager(QObject):
    """Single source of truth for the active theme.

    `theme_changed` fires with the new tokens after every apply; widgets that
    draw custom content (canvas, icons) connect to it.
    """

    theme_changed = Signal(object)  # ThemeTokens

    def __init__(self, settings: Settings, parent: QObject | None = None):
        super().__init__(parent)
        self.settings = settings
        self._tokens: ThemeTokens = LIGHT
        self._mode = "system"
        try:
            app = QApplication.instance()
            if app is not None:
                app.styleHints().colorSchemeChanged.connect(self._on_system_scheme_changed)
        except (AttributeError, RuntimeError):
            pass

    # -- state -------------------------------------------------------------
    @property
    def tokens(self) -> ThemeTokens:
        return self._tokens

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_dark(self) -> bool:
        return self._tokens.name == "dark"

    @property
    def fg_color(self) -> str:
        return self._tokens.text_main

    @property
    def accent_color(self) -> str:
        return self._tokens.accent

    # -- application ----------------------------------------------------------
    def apply(self, mode: str | None = None) -> ThemeTokens:
        """Apply `mode` ('light'|'dark'|'system') or re-apply the stored one."""
        self._mode = mode or self.settings.theme
        tokens = DARK if self._resolve_dark() else LIGHT
        self._tokens = tokens
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_qss(tokens))
            pal = app.palette()
            pal.setColor(QPalette.Window, QColor(tokens.bg_main))
            pal.setColor(QPalette.WindowText, QColor(tokens.text_main))
            pal.setColor(QPalette.Base, QColor(tokens.bg_input))
            pal.setColor(QPalette.Text, QColor(tokens.text_main))
            pal.setColor(QPalette.Button, QColor(tokens.bg_panel))
            pal.setColor(QPalette.ButtonText, QColor(tokens.text_main))
            pal.setColor(QPalette.Highlight, QColor(tokens.bg_selected))
            pal.setColor(QPalette.ToolTipBase, QColor(tokens.bg_tooltip))
            pal.setColor(QPalette.ToolTipText, QColor("#FFFFFF" if tokens.name == "light" else tokens.bg_panel))
            app.setPalette(pal)
        icons.clear_cache()
        self.theme_changed.emit(tokens)
        if self.settings.theme != self._mode:
            self.settings.theme = self._mode
        return tokens

    def _resolve_dark(self) -> bool:
        if self._mode == "dark":
            return True
        if self._mode == "light":
            return False
        return system_is_dark()

    def _on_system_scheme_changed(self, *_args) -> None:
        if self._mode == "system":
            self.apply("system")

    # -- icon helper -----------------------------------------------------------
    def icon(self, name: str, px: int = 20, color: str | None = None):
        return icons.icon(name, color or self._tokens.text_main, px)
