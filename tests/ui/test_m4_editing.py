"""M4 UI tests: paragraph box resize, enlarge-box overflow, Add Text tool."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint

from openpdfsuite.domain.models import EditMode
from openpdfsuite.ui.document_view import PAGE_MARGIN
from tests.fixtures.make_fixtures import PARA_LINE1, TRAILING
from tests.ui.test_shell import wait_opened


@pytest.fixture()
def w(qtbot, window, fixture_dir):
    window.open_document(str(fixture_dir / "standard.pdf"))
    wait_opened(qtbot, window)
    qtbot.waitUntil(lambda: window.view is not None
                    and bool(window.view.frames)
                    and window.view.frames[0].pixmap is not None, timeout=45000)
    return window


def enter_edit_mode(qtbot, w):
    w.action_tool_edit.trigger()
    assert w.view.mode == "edit"
    qtbot.waitUntil(lambda: bool(w.view.page_regions.get(0)), timeout=45000)
    qtbot.waitUntil(lambda: bool(w.view.page_lines.get(0)), timeout=45000)


def para_region(w):
    for r in w.view.page_regions[0]:
        if r.mode == EditMode.REFLOW_BOX and PARA_LINE1 in r.text:
            return r
    raise AssertionError("paragraph region not found")


def select_para(qtbot, w):
    region = para_region(w)
    w.view.region_clicked.emit(0, region)
    assert w._layout_box is not None
    assert w.view.layout_box_rect is not None
    return region


def drag_bottom_handle(w, new_bottom_pt):
    frame = w.view.frames[0]
    orig = w.view.layout_box_rect[1]
    mid_x = (orig[0] + orig[2]) / 2
    start = QPoint(int(PAGE_MARGIN + mid_x * frame.zoom),
                   int(PAGE_MARGIN + orig[3] * frame.zoom))
    w.view._box_drag = {"op": "resize", "handle": "b",
                        "start": frame.map_to_page(start), "orig": orig, "page": 0}
    target = QPoint(int(PAGE_MARGIN + mid_x * frame.zoom),
                    int(PAGE_MARGIN + new_bottom_pt * frame.zoom))
    w.view._update_box_drag(frame, target)
    w.view._frame_mouse_release(frame, None)


def test_para_selection_shows_layout_box(qtbot, w):
    enter_edit_mode(qtbot, w)
    select_para(qtbot, w)
    assert w.view.layout_box_rect[0] == 0
    # selecting a line region clears it again
    line = next(r for r in w.view.page_regions[0]
                if r.mode == EditMode.PRESERVE_LINE)
    w.view.region_clicked.emit(0, line)
    assert w.view.layout_box_rect is None


def test_resize_box_allows_longer_reflow(qtbot, w):
    enter_edit_mode(qtbot, w)
    select_para(qtbot, w)
    drag_bottom_handle(w, 230.0)
    assert w._layout_box[1][3] == pytest.approx(230.0, abs=1.0)
    region = para_region(w)
    w.view.region_double_clicked.emit(0, region)
    w._confirm_preview = lambda info: True
    w._overlay.editor.setPlainText("word " * 44)  # needs ~6 lines, not 4
    with qtbot.waitSignal(w.controller.edit_committed, timeout=60000):
        w._overlay.apply()
    with qtbot.waitSignal(w.controller.search_done, timeout=45000) as blocker:
        w.controller.search(PARA_LINE1)
    assert blocker.args[0] == []
    with qtbot.waitSignal(w.controller.search_done, timeout=45000) as blocker:
        w.controller.search(TRAILING)
    assert len(blocker.args[0]) == 1  # neighbor intact


def test_overflow_enlarge_flow(qtbot, w):
    enter_edit_mode(qtbot, w)
    select_para(qtbot, w)
    region = para_region(w)
    w.view.region_double_clicked.emit(0, region)
    w._confirm_preview = lambda info: True
    w._ask_overflow = lambda validation, can_enlarge_box=False: (
        "enlarge" if can_enlarge_box else "cancel")
    w._overlay.editor.setPlainText("word " * 46)
    with qtbot.waitSignal(w.controller.edit_committed, timeout=90000):
        w._overlay.apply()
    assert w.controller.session.revision == 1
    # the layout box grew into the empty gap (original bottom was ~194)
    assert w._layout_box[1][3] > 200.0
    with qtbot.waitSignal(w.controller.search_done, timeout=45000) as blocker:
        w.controller.search(TRAILING)
    assert len(blocker.args[0]) == 1


def test_add_text_flow(qtbot, w):
    w.action_tool_add.trigger()
    assert w.view.mode == "add"
    w.view.add_text_requested.emit(0, 90.0, 320.0)
    assert w._overlay is not None
    w._confirm_preview = lambda info: True
    w._overlay.editor.setPlainText("Brand new text block.")
    with qtbot.waitSignal(w.controller.edit_committed, timeout=60000):
        w._overlay.apply()
    assert w.controller.session.revision == 1
    with qtbot.waitSignal(w.controller.search_done, timeout=45000) as blocker:
        w.controller.search("Brand new text block.")
    assert len(blocker.args[0]) == 1 and blocker.args[0][0]["page"] == 0


def test_add_text_over_existing_text_refused(qtbot, w, monkeypatch):
    w.action_tool_add.trigger()
    w.view.add_text_requested.emit(0, 90.0, 70.0)  # right over the title
    assert w._overlay is not None
    messages = []
    monkeypatch.setattr(
        "openpdfsuite.ui.main_window.QMessageBox.warning",
        staticmethod(lambda *a, **k: messages.append(a[2] if len(a) > 2 else "")))
    w._overlay.editor.setPlainText("colliding text")
    w._overlay.apply()
    qtbot.waitUntil(lambda: bool(messages), timeout=45000)
    assert w.controller.session.revision == 0
    assert any("already has text" in m for m in messages)


def test_add_text_style_overrides(qtbot, w):
    w.action_tool_add.trigger()
    w.view.add_text_requested.emit(0, 90.0, 320.0)
    w.properties.size_spin.setValue(18.0)
    w._confirm_preview = lambda info: True
    w._overlay.editor.setPlainText("Large new text.")
    with qtbot.waitSignal(w.controller.edit_committed, timeout=60000):
        w._overlay.apply()
    with qtbot.waitSignal(w.controller.search_done, timeout=45000) as blocker:
        w.controller.search("Large new text.")
    assert len(blocker.args[0]) == 1
