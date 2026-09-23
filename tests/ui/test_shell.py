"""M1 shell tests: window, menus, toolbar, welcome screen, theme, open flow.

Opening is async (worker process); tests that need an opened document use the
`opened_window` helper which waits for the session to exist.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from openpdfsuite.ui.main_window import _looks_like_pdf


def wait_opened(qtbot, window, timeout=45000):
    """Block until the controller reports an open session."""
    qtbot.waitUntil(
        lambda: window.controller is not None and window.controller.session is not None,
        timeout=timeout,
    )
    return window


@pytest.fixture()
def opened_window(qtbot, window, fixture_dir):
    window.open_document(str(fixture_dir / "standard.pdf"))
    wait_opened(qtbot, window)
    return window


def test_window_basics(window):
    assert window.windowTitle() == "openPDF suite"
    assert window.minimumWidth() == 1100
    assert window.minimumHeight() == 720


def test_menus_present(window):
    titles = [a.text().replace("&", "") for a in window.menuBar().actions()]
    for expected in ("File", "Edit", "View", "Document", "Help"):
        assert expected in titles, f"missing menu {expected}; got {titles}"


def test_welcome_visible_initially(window):
    assert window.stack.currentWidget() is window.welcome
    btn = window.welcome.findChild(QPushButton, "WelcomeOpenButton")
    assert btn is not None and btn.isVisible()


def test_welcome_shows_brand_logo(window):
    """The welcome card uses the brand app_icon.png, not the vector fallback."""
    from PySide6.QtGui import QGuiApplication

    from openpdfsuite.ui.components.icons import logo_pixmap
    pm = window.welcome._logo_label.pixmap()
    brand = logo_pixmap(56)
    assert brand is not None, "brand asset src/openpdfsuite/resources/app_icon.png missing"
    dpr = QGuiApplication.primaryScreen().devicePixelRatio()
    assert pm.width() == brand.width() and pm.height() == brand.height()
    assert pm.devicePixelRatio() == dpr


def test_app_window_icon_is_brand(qtbot):
    """create_app installs the brand icon on the QApplication."""
    from PySide6.QtWidgets import QApplication

    from openpdfsuite.app import create_app
    from openpdfsuite.ui.components.icons import app_icon_path
    assert app_icon_path() is not None
    app = create_app([])
    assert app is QApplication.instance()
    assert not app.windowIcon().isNull()


def test_document_actions_disabled_without_doc(window):
    for a in (window.action_save, window.action_undo, window.action_redo,
              window.action_find, window.action_close_doc):
        assert not a.isEnabled(), a.text()
        # disabled with explanation
        assert "Open a document first" in a.toolTip(), a.text()


def test_view_actions_disabled_without_doc(window):
    for a in (window.action_zoom_in, window.action_zoom_out,
              window.action_fit_width, window.action_fit_page):
        assert not a.isEnabled()
        assert "Open a document first" in a.toolTip()


def test_edit_tools_state_without_doc(window):
    assert not window.action_tool_edit.isEnabled()
    assert not window.action_tool_add.isEnabled()
    assert "Open a document" in window.action_tool_add.toolTip()


def test_open_document_updates_state(qtbot, window, fixture_dir):
    pdf = fixture_dir / "standard.pdf"
    window.open_document(str(pdf))
    # sync part: title, recents, loading state
    assert window._current_path == pdf
    assert window.windowTitle().startswith("standard.pdf")
    assert window.stack.currentIndex() == 1
    assert str(pdf) in window.settings.recent_files()
    labels = [lbl.text() for lbl in window.stack.widget(1).findChildren(QLabel)]
    assert any("standard.pdf" in t for t in labels)
    # async part: session lands, view replaces the loading widget
    wait_opened(qtbot, window)
    qtbot.waitUntil(lambda: window.view is not None, timeout=5000)
    assert window.controller.session.page_count == 2
    assert window.action_close_doc.isEnabled()


def test_open_rejects_non_pdf(window, tmp_path):
    fake = tmp_path / "not_a.pdf"
    fake.write_bytes(b"hello world, definitely not a pdf")
    assert not _looks_like_pdf(fake)
    window.open_document(str(fake))
    assert window._current_path is None
    assert window.stack.currentIndex() == 0


def test_open_missing_file_clears_recent(window, tmp_path):
    ghost = tmp_path / "ghost.pdf"
    window.settings.push_recent(str(ghost))
    window.open_document(str(ghost))
    assert str(ghost).lower() not in [r.lower() for r in window.settings.recent_files()]


def test_close_document_returns_to_welcome(opened_window):
    window = opened_window
    window._on_close_doc()
    assert window.stack.currentWidget() is window.welcome
    assert window._current_path is None
    assert window.windowTitle() == "openPDF suite"
    assert window.controller is None


def test_theme_switch(window, theme):
    theme.apply("dark")
    assert theme.tokens.name == "dark"
    assert theme.tokens.bg_main == "#15181E"
    theme.apply("light")
    assert theme.tokens.name == "light"
    assert theme.tokens.bg_main == "#F3F5F8"


def test_theme_persisted(window, settings, theme):
    theme.apply("dark")
    assert settings.theme == "dark"
    theme.apply("system")
    assert settings.theme == "system"


def test_sidebar_toggle(window, qtbot):
    assert window.sidebar.isVisible()
    assert window.action_sidebar.isChecked()
    window.action_sidebar.trigger()  # checkable: toggles to unchecked -> hides
    qtbot.wait(50)
    assert not window.sidebar.isVisible()
    window.action_sidebar.trigger()
    qtbot.wait(50)
    assert window.sidebar.isVisible()


def test_search_enabled_after_open(qtbot, window, fixture_dir):
    assert not window.sidebar_tabs.search.field.isEnabled()
    window.open_document(str(fixture_dir / "standard.pdf"))
    wait_opened(qtbot, window)
    qtbot.waitUntil(lambda: window.sidebar_tabs.search.field.isEnabled(), timeout=5000)
    assert window.action_find.isEnabled()


def test_recents_menu_populates(window, fixture_dir):
    window.open_document(str(fixture_dir / "standard.pdf"))
    window._populate_recents()
    texts = [a.text() for a in window.menu_recent.actions()]
    assert any("standard.pdf" in t for t in texts)


def test_statusbar_shows_document(qtbot, window, fixture_dir):
    window.open_document(str(fixture_dir / "standard.pdf"))
    assert "standard.pdf" in window.page_label.text()  # "Opening …" at minimum
    wait_opened(qtbot, window)
    qtbot.waitUntil(lambda: "Page 1 of 2" in window.page_label.text(), timeout=5000)


def test_limitations_dialog_text(window):
    # dialog content is built inline; just ensure the action exists and is enabled
    assert window.action_limits.isEnabled()
    assert window.action_about.isEnabled()
