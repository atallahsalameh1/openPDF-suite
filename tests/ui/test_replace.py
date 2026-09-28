"""M10 Replace All UI tests: dialog stages, review window, apply, undo.

Drives the real ReplaceDialog against the real worker process (no stubs on
the plan path) and verifies document truth through fresh searches, the same
way the M9 redaction UI tests do.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from openpdfsuite.ui.dialogs.replace_dialog import ReplaceDialog
from tests.ui.test_shell import wait_opened


@pytest.fixture()
def w(qtbot, window, fixture_dir):
    """Window with standard.pdf open and first page rendered."""
    window.open_document(str(fixture_dir / "standard.pdf"))
    wait_opened(qtbot, window)
    qtbot.waitUntil(lambda: window.view is not None
                    and bool(window.view.frames)
                    and window.view.frames[0].pixmap is not None, timeout=45000)
    return window


@pytest.fixture()
def dlg(w, qtbot):
    d = ReplaceDialog(w, w.theme, w.controller)
    qtbot.addWidget(d)
    d.apply_requested.connect(w._on_replace_apply_started)
    d.show()
    return d


def test_replace_action_exists_with_shortcut(w):
    assert w.action_replace.isEnabled(), "Replace is enabled while a document is open"
    assert w.action_replace.shortcut().toString() == "Ctrl+H"


def test_review_and_apply_replaces_everywhere(qtbot, w, dlg):
    dlg.find_edit.setText("quick")
    dlg.replace_edit.setText("fast")
    dlg._on_review()

    qtbot.waitUntil(lambda: dlg._plan is not None, timeout=60000)
    assert dlg.match_list.count() == 2
    assert len(dlg._checked_ids()) == 2
    assert "2 of 2" in dlg.summary.text()
    assert dlg.apply_btn.isEnabled()

    dlg.apply_btn.click()
    qtbot.waitUntil(lambda: w.controller.session.revision == 1, timeout=30000)
    qtbot.waitUntil(lambda: "Replaced 2 match(es)" in w.statusBar().currentMessage(),
                    timeout=10000)

    # document truth via fresh searches: old word gone, new word on 2 lines
    results: list[list] = []
    w.controller.search_done.connect(lambda res, trunc: results.append(res))
    w.controller.search("quick")
    qtbot.waitUntil(lambda: len(results) > 0, timeout=15000)
    assert results[-1] == [], "replaced-away word must not be findable"
    w.controller.search("fast")
    qtbot.waitUntil(lambda: len(results) > 1, timeout=15000)
    assert len(results[-1]) == 2

    # one Ctrl+Z undoes the whole batch
    w._on_undo()
    qtbot.waitUntil(lambda: w.controller.session.revision == 0, timeout=30000)
    results.clear()
    w.controller.search("quick")
    qtbot.waitUntil(lambda: len(results) > 0, timeout=15000)
    assert len(results[-1]) == 2, "undo must restore every replaced line"


def test_unchecking_one_match_leaves_it_untouched(qtbot, w, dlg):
    dlg.find_edit.setText("quick")
    dlg.replace_edit.setText("fast")
    dlg._on_review()
    qtbot.waitUntil(lambda: dlg._plan is not None, timeout=60000)

    dlg.match_list.item(0).setCheckState(Qt.Unchecked)
    assert dlg.apply_btn.text() == "Apply 1 match(es)"

    dlg.apply_btn.click()
    qtbot.waitUntil(lambda: w.controller.session.revision == 1, timeout=30000)

    results: list[list] = []
    w.controller.search_done.connect(lambda res, trunc: results.append(res))
    w.controller.search("quick")
    qtbot.waitUntil(lambda: len(results) > 0, timeout=15000)
    assert len(results[-1]) == 1, "the unchecked match must survive"


def test_unrenderable_replacement_shows_skipped(qtbot, w, dlg):
    dlg.find_edit.setText("quick")
    dlg.replace_edit.setText("日本語")
    dlg._on_review()
    qtbot.waitUntil(lambda: dlg._plan is not None, timeout=60000)

    assert dlg.match_list.count() == 2
    item = dlg.match_list.item(0)
    assert not (item.flags() & Qt.ItemIsUserCheckable), "skipped matches can't be checked"
    assert "skipped" in item.text()
    assert "cannot render" in item.toolTip()
    assert not dlg.apply_btn.isEnabled(), "nothing applicable -> Apply disabled"
    assert "skipped" in dlg.summary.text().lower()


def test_no_matches_disables_apply(qtbot, w, dlg):
    dlg.find_edit.setText("zzzz-not-in-document")
    dlg.replace_edit.setText("x")
    dlg._on_review()
    qtbot.waitUntil(lambda: dlg._plan is not None, timeout=60000)

    assert dlg.match_list.count() == 0
    assert "No matches found" in dlg.summary.text()
    assert not dlg.apply_btn.isEnabled()
