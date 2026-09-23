"""M2 viewer tests: async open -> render -> navigate -> search through the UI."""

from __future__ import annotations

import pytest

from tests.ui.test_shell import wait_opened


@pytest.fixture()
def viewer(qtbot, window, fixture_dir):
    """Window with standard.pdf fully opened and first page rendered."""
    window.open_document(str(fixture_dir / "standard.pdf"))
    wait_opened(qtbot, window)
    qtbot.waitUntil(lambda: window.view is not None, timeout=10000)
    view = window.view
    qtbot.waitUntil(lambda: bool(view.frames) and view.frames[0].pixmap is not None,
                    timeout=30000)
    return window


def test_first_page_rendered(viewer):
    view = viewer.view
    assert len(view.frames) == 2
    pix = view.frames[0].pixmap
    assert pix is not None and not pix.isNull()
    assert pix.width() > 100


def test_page_and_zoom_status(viewer):
    assert "Page 1 of 2" in viewer.page_label.text()
    assert viewer.zoom_label.text().strip().endswith("%")


def test_thumbnails_arrive(qtbot, viewer):
    thumbs = viewer.sidebar_tabs.thumbnails
    assert thumbs.list.count() == 2
    qtbot.waitUntil(
        lambda: not thumbs.list.item(0).icon().isNull(), timeout=30000)


def test_zoom_in_requests_new_render(qtbot, viewer):
    view = viewer.view
    old_zoom = view.zoom
    viewer.action_zoom_in.trigger()
    qtbot.waitUntil(lambda: view.zoom > old_zoom, timeout=5000)
    qtbot.waitUntil(lambda: view.frames[0].pixmap is not None, timeout=30000)
    assert "%" in viewer.zoom_label.text()


def test_fit_page_changes_zoom(qtbot, viewer):
    view = viewer.view
    viewer.action_fit_page.trigger()
    qtbot.wait(200)
    assert 0.1 <= view.zoom <= 8.0


def test_scroll_to_page_updates_status(qtbot, viewer):
    viewer._goto_page(1)
    qtbot.waitUntil(lambda: "Page 2 of 2" in viewer.page_label.text(), timeout=5000)
    assert viewer.sidebar_tabs.thumbnails.list.currentRow() == 1


def test_search_end_to_end(qtbot, viewer):
    panel = viewer.sidebar_tabs.search
    panel.field.setText("fox")
    panel._submit()
    qtbot.waitUntil(lambda: panel.results.count() > 0, timeout=30000)
    assert "p.1" in panel.results.item(0).text()
    # highlights pushed to the view
    assert 0 in viewer.view.search_highlights
    assert viewer.view.search_highlights[0]


def test_search_no_results_state(qtbot, viewer):
    panel = viewer.sidebar_tabs.search
    panel.field.setText("zzzqqq-not-in-document")
    panel._submit()
    qtbot.waitUntil(lambda: panel.status.text().startswith("0 results")
                    or "No matches" in panel.empty.text(), timeout=30000)
    assert panel.results.count() == 0


def test_search_result_navigates(qtbot, viewer):
    panel = viewer.sidebar_tabs.search
    panel.field.setText("Second page")
    panel._submit()
    qtbot.waitUntil(lambda: panel.results.count() > 0, timeout=30000)
    panel.results.setCurrentRow(0)
    item = panel.results.item(0)
    panel._on_result(item)
    qtbot.waitUntil(lambda: "Page 2 of 2" in viewer.page_label.text(), timeout=5000)


def test_page_lines_arrive_for_selection(qtbot, viewer):
    view = viewer.view
    qtbot.waitUntil(lambda: 0 in view.page_lines and len(view.page_lines[0]) > 3,
                    timeout=30000)
    texts = [ln[0] for ln in view.page_lines[0]]
    assert any("Quarterly Report 2026" in t for t in texts)


def test_copy_selection_uses_clipboard(qtbot, viewer):
    view = viewer.view
    qtbot.waitUntil(lambda: 0 in view.page_lines, timeout=30000)
    view.selected_text = "Quarterly Report 2026"
    view.copy_selection()
    from PySide6.QtWidgets import QApplication

    assert QApplication.clipboard().text() == "Quarterly Report 2026"


def test_second_page_rendered_on_scroll(qtbot, viewer):
    view = viewer.view
    viewer._goto_page(1)
    qtbot.waitUntil(lambda: view.frames[1].pixmap is not None, timeout=30000)


def test_close_then_reopen(qtbot, viewer, fixture_dir):
    viewer._on_close_doc()
    assert viewer.controller is None
    viewer.open_document(str(fixture_dir / "standard.pdf"))
    wait_opened(qtbot, viewer)
    qtbot.waitUntil(lambda: viewer.view is not None
                    and bool(viewer.view.frames)
                    and viewer.view.frames[0].pixmap is not None, timeout=45000)
