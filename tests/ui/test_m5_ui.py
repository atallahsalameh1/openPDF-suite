"""M5 UI tests: recovery flow + unsupported-region diagnostics."""

from __future__ import annotations

import pymupdf
import pytest

from openpdfsuite.domain.models import EditMode
from openpdfsuite.infrastructure.recovery import RecoveryStore
from tests.fixtures.make_fixtures import TRAILING
from tests.ui.test_shell import wait_opened


@pytest.fixture()
def w(qtbot, window, fixture_dir):
    window.open_document(str(fixture_dir / "standard.pdf"))
    wait_opened(qtbot, window)
    qtbot.waitUntil(lambda: window.view is not None
                    and bool(window.view.frames)
                    and window.view.frames[0].pixmap is not None, timeout=45000)
    return window


def _commit_simple_edit(qtbot, w, new_text="Recovered world."):
    w.action_tool_edit.trigger()
    qtbot.waitUntil(lambda: bool(w.view.page_regions.get(0)), timeout=45000)
    region = next(r for r in w.view.page_regions[0]
                  if r.mode == EditMode.PRESERVE_LINE and r.text.strip() == TRAILING)
    w.view.region_double_clicked.emit(0, region)
    w._confirm_preview = lambda info: True
    w._overlay.editor.setPlainText(new_text)
    with qtbot.waitSignal(w.controller.edit_committed, timeout=60000):
        w._overlay.apply()


def test_recovery_written_after_commit(qtbot, w, tmp_path):
    store = RecoveryStore(tmp_path)
    w.recovery = store
    _commit_simple_edit(qtbot, w)
    w._write_recovery()  # debounce skipped in test
    qtbot.waitUntil(lambda: bool(store.list()), timeout=45000)
    entry = store.list()[0]
    assert entry.revision == 1
    assert "standard.pdf" in entry.source_path
    assert not entry.stale
    data = store.load_bytes(entry.session_id)
    check = pymupdf.open(stream=data)
    assert "Recovered world." in check[0].get_text()
    check.close()


def test_recovery_restore_flow(qtbot, window, fixture_dir, tmp_path):
    # simulate a crashed session: original bytes stand in for edited bytes
    store = RecoveryStore(tmp_path)
    data = (fixture_dir / "standard.pdf").read_bytes()
    src = tmp_path / "crashed.pdf"
    src.write_bytes(data)
    from openpdfsuite.infrastructure.pdf.saver import fingerprint_file

    store.write("crash-1", str(src), fingerprint_file(src), 2, 0, data)
    window.recovery = store
    window._ask_recovery = lambda entry, name, note: "restore"
    window._check_recovery()
    qtbot.waitUntil(lambda: window.controller is not None
                    and window.controller.session is not None, timeout=45000)
    wait_opened(qtbot, window)
    s = window.controller.session
    assert s.dirty and s.revision == 2 and s.saved_revision == 0
    assert not s.can_undo  # undo history does not survive a restart
    assert "•" in window.windowTitle()
    assert not window.action_undo.isEnabled()

    # saving removes the recovery entry
    dest = tmp_path / "restored_save.pdf"
    with qtbot.waitSignal(window.controller.saved, timeout=45000):
        window.controller.save(str(dest))
    qtbot.wait(200)
    assert store.list() == []


def test_recovery_discard_flow(qtbot, window, fixture_dir, tmp_path):
    store = RecoveryStore(tmp_path)
    data = (fixture_dir / "standard.pdf").read_bytes()
    store.write("crash-2", str(fixture_dir / "standard.pdf"), "fp", 1, 0, data)
    window.recovery = store
    window._ask_recovery = lambda entry, name, note: "discard"
    window._check_recovery()
    qtbot.wait(200)
    assert store.list() == []
    assert window.controller is None  # nothing opened


def test_unsupported_region_diagnostics(qtbot, window, fixture_dir, monkeypatch):
    window.open_document(str(fixture_dir / "existing_redaction.pdf"))
    wait_opened(qtbot, window)
    window.action_tool_edit.trigger()
    qtbot.waitUntil(lambda: bool(window.view.page_regions.get(0)), timeout=45000)
    regions = window.view.page_regions[0]
    assert regions and all(not r.editable for r in regions)
    assert all("redaction" in r.unsupported_reason for r in regions)

    region = regions[0]
    window.view.region_clicked.emit(0, region)
    assert window._selected_region is None  # not selected for editing

    messages = []
    monkeypatch.setattr(
        "openpdfsuite.ui.main_window.QMessageBox.information",
        staticmethod(lambda *a, **k: messages.append(a[2] if len(a) > 2 else "")))
    window.view.region_double_clicked.emit(0, region)
    assert any("redaction" in m for m in messages)  # reason text, not title
    assert window._overlay is None
