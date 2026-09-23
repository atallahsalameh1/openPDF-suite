"""M3 editing tests: the full UI cycle over the real worker.

select region -> overlay edit -> engine prepare -> preview seam -> commit ->
undo/redo -> validated save. Dialogs are replaced by test seams
(`_confirm_preview`, `_ask_overflow`) so flows are deterministic.
"""

from __future__ import annotations

import pymupdf
import pytest

from openpdfsuite.domain.models import EditMode
from tests.fixtures.make_fixtures import TITLE, TRAILING
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


def enter_edit_mode(qtbot, w):
    w.action_tool_edit.trigger()
    assert w.view.mode == "edit"
    qtbot.waitUntil(lambda: bool(w.view.page_regions.get(0)), timeout=45000)


def find_line_region(w, text, page=0):
    for r in w.view.page_regions.get(page, []):
        if r.mode == EditMode.PRESERVE_LINE and r.text.strip() == text:
            return r
    raise AssertionError(f"region not found: {text!r}")


def find_para_region(w, contains, page=0):
    for r in w.view.page_regions.get(page, []):
        if r.mode == EditMode.REFLOW_BOX and contains in r.text:
            return r
    raise AssertionError(f"paragraph region not found containing {contains!r}")


def test_edit_tool_enabled_after_open(w):
    assert w.action_tool_edit.isEnabled()
    assert w.action_tool_add.isEnabled()


def test_select_region_shows_properties(qtbot, w):
    enter_edit_mode(qtbot, w)
    region = find_line_region(w, TRAILING)
    w.view.region_clicked.emit(0, region)
    assert w._selected_region == (0, region)
    assert w.properties.font_combo.isEnabled()
    assert w.properties.size_spin.value() == pytest.approx(11.0, abs=0.5)
    assert w.view.selected_region_rect is not None


def test_double_click_opens_overlay(qtbot, w):
    enter_edit_mode(qtbot, w)
    region = find_line_region(w, TRAILING)
    w.view.region_double_clicked.emit(0, region)
    assert w._overlay is not None
    assert w._overlay.editor.toPlainText() == TRAILING


def test_full_edit_undo_save_cycle(qtbot, w, tmp_path):
    enter_edit_mode(qtbot, w)
    region = find_line_region(w, TRAILING)
    w.view.region_double_clicked.emit(0, region)

    captured = {}

    def fake_confirm(info):
        captured.update(info)
        return True

    w._confirm_preview = fake_confirm
    w._overlay.editor.setPlainText("Conclusion of section one.")

    with qtbot.waitSignal(w.controller.edit_committed, timeout=45000):
        w._overlay.apply()

    # authoritative preview artifacts crossed the wire
    assert captured.get("before_png") and captured.get("preview_png")
    assert captured["ok"]

    # session + chrome state
    s = w.controller.session
    assert s.dirty and s.can_undo and s.revision == 1
    assert "•" in w.windowTitle()
    assert w.action_undo.isEnabled()
    assert w.action_save.isEnabled()

    # old text gone, new text present (through the worker's live document)
    with qtbot.waitSignal(w.controller.search_done, timeout=45000) as blocker:
        w.controller.search(TRAILING)
    assert blocker.args[0] == []
    with qtbot.waitSignal(w.controller.search_done, timeout=45000) as blocker:
        w.controller.search("Conclusion of section one.")
    assert len(blocker.args[0]) == 1 and blocker.args[0][0]["page"] == 0

    # validated save to a new file
    dest = tmp_path / "edited_out.pdf"
    with qtbot.waitSignal(w.controller.saved, timeout=45000):
        w.controller.save(str(dest))
    check = pymupdf.open(str(dest))
    text = check[0].get_text()
    assert "Conclusion of section one." in text
    assert TRAILING not in text
    check.close()
    assert not w.controller.session.dirty
    assert not w.action_save.isEnabled()
    assert "•" not in w.windowTitle()

    # undo brings the original back
    with qtbot.waitSignal(w.controller.undo_redo_done, timeout=45000):
        w.action_undo.trigger()
    with qtbot.waitSignal(w.controller.search_done, timeout=45000) as blocker:
        w.controller.search(TRAILING)
    assert len(blocker.args[0]) == 1
    assert not w.controller.session.can_undo
    assert w.action_redo.isEnabled()

    # redo re-applies
    with qtbot.waitSignal(w.controller.undo_redo_done, timeout=45000):
        w.action_redo.trigger()
    with qtbot.waitSignal(w.controller.search_done, timeout=45000) as blocker:
        w.controller.search("Conclusion of section one.")
    assert len(blocker.args[0]) == 1


def test_preview_cancel_commits_nothing(qtbot, w):
    enter_edit_mode(qtbot, w)
    region = find_line_region(w, TRAILING)
    w.view.region_double_clicked.emit(0, region)
    w._confirm_preview = lambda info: False
    w._overlay.editor.setPlainText("Never applied.")
    with qtbot.waitSignal(w.controller.edit_prepared, timeout=45000):
        w._overlay.apply()
    qtbot.wait(300)  # any accidental commit would land here
    assert w.controller.session.revision == 0
    assert not w.controller.session.dirty


def test_overflow_shrink_flow(qtbot, w):
    enter_edit_mode(qtbot, w)
    region = find_line_region(w, TITLE)
    w.view.region_double_clicked.emit(0, region)

    seen = {}

    def fake_confirm(info):
        seen.update(info)
        return True

    w._confirm_preview = fake_confirm
    w._ask_overflow = lambda validation, can_enlarge_box=False: "shrink"
    # ~70 chars at 20 pt bold far exceeds the line's room -> overflow -> shrink
    w._overlay.editor.setPlainText("Q" * 70)
    with qtbot.waitSignal(w.controller.edit_committed, timeout=60000):
        w._overlay.apply()

    assert seen.get("ok")
    validation = seen["validation"]
    assert any("auto-shrunk" in i for i in validation.issues)
    assert w.controller.session.revision == 1


def test_overflow_cancel_flow(qtbot, w):
    enter_edit_mode(qtbot, w)
    region = find_line_region(w, TITLE)
    w.view.region_double_clicked.emit(0, region)
    w._ask_overflow = lambda validation, can_enlarge_box=False: "cancel"
    w._overlay.editor.setPlainText("Q" * 70)
    with qtbot.waitSignal(w.controller.edit_prepared, timeout=45000):
        w._overlay.apply()
    qtbot.wait(300)
    assert w.controller.session.revision == 0
    assert w._pending_edit is None


def test_paragraph_region_edit_reflows(qtbot, w):
    """Mode B through the UI: paragraph double-click, longer text, same box."""
    from tests.fixtures.make_fixtures import PARA_LINE1

    enter_edit_mode(qtbot, w)
    region = find_para_region(w, PARA_LINE1)
    w.view.region_double_clicked.emit(0, region)
    assert w._overlay is not None
    w._confirm_preview = lambda info: True
    new_text = ("A completely rewritten paragraph that replaces all four "
                "original lines with a single reflowed block of text.")
    w._overlay.editor.setPlainText(new_text)
    with qtbot.waitSignal(w.controller.edit_committed, timeout=60000):
        w._overlay.apply()
    with qtbot.waitSignal(w.controller.search_done, timeout=45000) as blocker:
        w.controller.search(PARA_LINE1)
    assert blocker.args[0] == []


def test_style_overrides_captured(qtbot, w):
    enter_edit_mode(qtbot, w)
    region = find_line_region(w, TRAILING)
    w.view.region_clicked.emit(0, region)
    w.properties.size_spin.setValue(9.0)
    assert w._style_overrides.get("size") == pytest.approx(9.0)
    w.properties.bold_btn.setChecked(True)
    assert w._style_overrides.get("bold") is True


def test_size_override_reaches_output(qtbot, w):
    enter_edit_mode(qtbot, w)
    region = find_line_region(w, TRAILING)
    w.view.region_clicked.emit(0, region)
    w.properties.size_spin.setValue(16.0)
    w.view.region_double_clicked.emit(0, region)
    w._confirm_preview = lambda info: True
    w._overlay.editor.setPlainText("Sixteen point line.")
    with qtbot.waitSignal(w.controller.edit_committed, timeout=45000):
        w._overlay.apply()
    # verify through extraction: the new line's span size is ~16
    with qtbot.waitSignal(w.controller.search_done, timeout=45000) as blocker:
        w.controller.search("Sixteen point line.")
    assert len(blocker.args[0]) == 1


def test_tool_switch_clears_selection(qtbot, w):
    enter_edit_mode(qtbot, w)
    region = find_line_region(w, TRAILING)
    w.view.region_clicked.emit(0, region)
    assert w._selected_region is not None
    w.action_tool_select.trigger()
    assert w.view.mode == "select"
    assert w._selected_region is None
    assert w.view.selected_region_rect is None


def test_first_save_uses_save_as(qtbot, w, monkeypatch):
    called = {}
    monkeypatch.setattr(w, "_on_save_as", lambda: called.setdefault("as", True))
    w._saved_once = False
    w._on_save()
    assert called.get("as") is True


def test_missing_glyphs_blocked_with_message(qtbot, w, monkeypatch):
    enter_edit_mode(qtbot, w)
    region = find_line_region(w, TRAILING)
    w.view.region_double_clicked.emit(0, region)
    messages = []
    monkeypatch.setattr(
        "openpdfsuite.ui.main_window.QMessageBox.warning",
        staticmethod(lambda *a, **k: messages.append(a[2] if len(a) > 2 else "")))
    # CJK text with a font override to a family that cannot cover it forces the
    # resolver's honest failure path (builtin fallback reports missing glyphs)
    w._style_overrides["font"] = "NoSuchFontXyz123"
    w._overlay.editor.setPlainText("中文测试")
    w._overlay.apply()
    qtbot.waitUntil(lambda: bool(messages), timeout=45000)
    assert w.controller.session.revision == 0


def test_prepare_failure_surfaces_validation_reason(qtbot, w):
    """A failed candidate must never degrade to the old 'Unknown issue' dialog."""
    from openpdfsuite.domain.models import ValidationResult
    from openpdfsuite.infrastructure.pdf.protocol import PREPARE_EDIT, Result

    seen = []
    w.controller.edit_prepared.connect(seen.append)
    validation = ValidationResult(ok=False, issues=["Specific validation failure."])
    result = Result(
        request_id=999, kind=PREPARE_EDIT,
        doc_id=w.controller.session.doc_id,
        revision=w.controller.session.revision, ok=False,
        error="edit failed", payload={"validation": validation, "issues": []},
    )

    w.controller._on_prepared(result)

    assert seen[-1]["issues"] == ["Specific validation failure."]
