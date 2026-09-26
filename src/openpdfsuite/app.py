"""Application bootstrap: QApplication, logging, theme, first window."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from .infrastructure.logging import get_logger, setup_logging
from .infrastructure.settings import Settings
from .ui.components.icons import app_icon
from .ui.main_window import MainWindow
from .ui.themes import ThemeManager

log = get_logger("app")

_windows: list[MainWindow] = []


def _pick_font() -> QFont:
    """Segoe UI Variable when available, falling back to Segoe UI (AGENTS.md §4)."""
    families = set(QFontDatabase.families())
    for name in ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"):
        if name in families:
            font = QFont(name)
            font.setPointSize(9)
            font.setHintingPreference(QFont.PreferFullHinting)
            return font
    font = QFont()
    font.setPointSize(9)
    return font


def create_app(argv: list[str] | None = None) -> QApplication:
    argv = argv if argv is not None else sys.argv
    # per-monitor DPI awareness is on by default in Qt 6; use fractional rounding
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("OpenPDFSuite")
    app.setApplicationDisplayName("openPDF suite")
    app.setOrganizationName("OpenPDFSuite")
    app.setWindowIcon(app_icon())  # brand mark in title bar / taskbar
    app.setFont(_pick_font())
    _install_excepthook()
    return app


def _install_excepthook() -> None:
    """Log uncaught exceptions (including Qt slot errors) to the app log.

    Without this, an exception raised in a Qt slot only reaches the console —
    invisible for a windowed launch — and the UI silently does nothing.
    """
    import traceback

    from .infrastructure.logging import get_logger

    log = get_logger("ui.errors")

    def _hook(exc_type, exc, tb) -> None:
        log.error("uncaught exception:\n%s", "".join(traceback.format_exception(
            exc_type, exc, tb)))

    sys.excepthook = _hook


def open_window(path: str | None = None) -> MainWindow:
    app = QApplication.instance()
    assert app is not None, "create_app() must run first"
    settings = getattr(app, "_suite_settings", None)
    theme = getattr(app, "_suite_theme", None)
    if settings is None:
        settings = Settings()
        app._suite_settings = settings  # type: ignore[attr-defined]
    if theme is None:
        theme = ThemeManager(settings)
        app._suite_theme = theme  # type: ignore[attr-defined]
        theme.apply()
    win = MainWindow(settings, theme, open_path=path)
    _windows.append(win)
    win.destroyed.connect(lambda *_: _windows.remove(win) if win in _windows else None)
    win.show()
    return win


def main(argv: list[str] | None = None) -> int:
    log_file = setup_logging()
    log.info("starting openPDF suite, log at %s", log_file)
    app = create_app(argv)
    args = list(argv or sys.argv)
    path = None
    for a in args[1:]:
        if a.lower().endswith(".pdf"):
            path = a
            break
    open_window(path)
    return app.exec()
