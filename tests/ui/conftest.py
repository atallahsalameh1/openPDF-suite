"""UI test fixtures: isolated settings (no registry pollution), theme, window."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings

from openpdfsuite.infrastructure.settings import Settings
from openpdfsuite.ui.themes import ThemeManager


@pytest.fixture(scope="session")
def recovery_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("recovery")


@pytest.fixture(scope="session", autouse=True)
def isolate_settings(tmp_path_factory, recovery_dir):
    ini_dir = tmp_path_factory.mktemp("qsettings")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(ini_dir))
    QSettings.setPath(QSettings.IniFormat, QSettings.SystemScope, str(ini_dir))
    # recovery must never read/write the user's real %APPDATA% during tests:
    # a stale real entry would pop a modal restore prompt and hang the suite
    import openpdfsuite.ui.main_window as mw
    from openpdfsuite.infrastructure.recovery import RecoveryStore

    real_store = mw.RecoveryStore
    mw.RecoveryStore = lambda base=None: RecoveryStore(recovery_dir)
    yield
    mw.RecoveryStore = real_store


@pytest.fixture()
def settings() -> Settings:
    s = Settings()
    s.clear_recents()
    return s


@pytest.fixture()
def theme(qapp, settings) -> ThemeManager:
    tm = ThemeManager(settings)
    tm.apply("light")
    return tm


@pytest.fixture()
def window(qtbot, settings, theme, recovery_dir):
    from openpdfsuite.ui.main_window import MainWindow

    # a leftover entry from a previous test would pop the restore prompt
    for leftover in recovery_dir.iterdir():
        leftover.unlink()
    w = MainWindow(settings, theme)
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    return w
