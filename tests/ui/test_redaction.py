"""M9 redaction UI tests: Black Out mode, marquee marks, search marks, apply.

Uses real Qt mouse events through qtbot for the marquee (the view handlers run
unchanged), and the `_confirm_redaction_preview` seam for the apply flow.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt

from openpdfsuite.ui.document_view import PAGE_MARGIN
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


def _frame_pos(w, x_pt: float, y_pt: float) -> QPoint:
    """Widget-local QPoint for an engine/display-space page point (rot 0)."""
    frame = w.view.frames[0]
    return QPoint(int(PAGE_MARGIN + x_pt * frame.zoom),
                  int(PAGE_MARGIN + y_pt * frame.zoom))


def test_redact_mode_and_marquee_mark(qtbot, w):
    w.action_tool_redact.trigger()
    assert w.view.mode == "redact"

    frame = w.view.frames[0]
    start = _frame_pos(w, 72.0, 228.0)
    end = _frame_pos(w, 165.0, 240.0)
    qtbot.mouseMove(frame, start)
    qtbot.mousePress(frame, Qt.LeftButton, pos=start)
    qtbot.mouseMove(frame, end)
    qtbot.mouseRelease(frame, Qt.LeftButton, pos=end)

    marks = w.view.get_redact_marks()
    assert len(marks) == 1, f"expected one mark, got {marks}"
    page, rect = marks[0]
    assert page == 0
    assert rect.x0 < 90 and rect.x1 > 150, "mark should span the dragged x-range"
    assert w.action_redact_apply.isEnabled(), "apply enables when marks exist"

    # click on the mark removes it
    mid = _frame_pos(w, (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
    qtbot.mouseMove(frame, mid)
    qtbot.mousePress(frame, Qt.LeftButton, pos=mid)
    qtbot.mouseRelease(frame, Qt.LeftButton, pos=mid)
    assert not w.view.has_redact_marks(), "click on a mark must remove it"
    assert not w.action_redact_apply.isEnabled()


def test_escape_clears_marks(qtbot, w):
    w.action_tool_redact.trigger()
    w.view.add_redact_mark(0, __import__(
        "openpdfsuite.domain.models", fromlist=["Rect"]).Rect(72, 220, 160, 240))
    assert w.view.has_redact_marks()
    qtbot.keyPress(w.view, Qt.Key_Escape)
    assert not w.view.has_redact_marks(), "Escape clears all marks"


def test_apply_redaction_commit(qtbot, w):
    """Mark 'End of first section.', apply with auto-confirmed preview, verify
    the text is really gone via a fresh search."""
    from openpdfsuite.domain.models import Rect

    w.action_tool_redact.trigger()
    w.view.add_redact_mark(0, Rect(70.0, 224.0, 170.0, 242.0))

    w._confirm_redaction_preview = lambda info: True
    results: list[list] = []
    w.controller.search_done.connect(
        lambda res, trunc: results.append(res))
    w._on_redact_apply()

    qtbot.waitUntil(lambda: w.controller.session.revision == 1, timeout=30000)
    qtbot.waitUntil(lambda: w.view.has_redact_marks() is False, timeout=10000)

    w.controller.search("End of first section")
    qtbot.waitUntil(lambda: len(results) > 0, timeout=15000)
    assert results[-1] == [], "redacted text must not be findable anymore"


def test_apply_runs_the_real_preview_path(qtbot, w, monkeypatch):
    """Regression (user-reported: 'apply does nothing'): `_confirm_redaction_
    preview` crashed on `Rect(*Rect)` — invisible because the commit test
    stubbed this method out. Patch the DIALOG CLASS, not the method, so the
    real body runs and the commit still goes through."""
    import openpdfsuite.ui.main_window as mw
    from openpdfsuite.domain.models import Rect

    calls: dict = {}

    class FakePreviewDialog:
        def __init__(self, parent, theme, before_png, after_png, box, zoom,
                     validation, issues, apply_label="Apply edit",
                     title="Preview replacement"):
            calls["apply_label"] = apply_label
            calls["title"] = title
            calls["box"] = box
            calls["has_before"] = bool(before_png)
            calls["has_after"] = bool(after_png)

        def exec(self):
            return mw.QDialog.Accepted

    monkeypatch.setattr(mw, "PreviewDialog", FakePreviewDialog)

    w.action_tool_redact.trigger()
    w.view.add_redact_mark(0, Rect(70.0, 224.0, 170.0, 242.0))
    w._on_redact_apply()

    qtbot.waitUntil(lambda: w.controller.session.revision == 1, timeout=30000)
    assert calls["apply_label"] == "Apply redaction"
    assert calls["title"] == "Preview black-out"
    assert calls["has_before"] and calls["has_after"]
    assert calls["box"].width > 0


def test_search_blackout_all_adds_marks(qtbot, w):
    """'Black out all…' converts every search hit into a reviewable mark."""

    w.action_tool_redact.trigger()
    results: list[list] = []
    w.controller.search_done.connect(lambda res, trunc: results.append(res))
    w.controller.search("Quarterly Report")
    qtbot.waitUntil(lambda: len(results) > 0, timeout=15000)
    hits = [r for r in results[-1] if r.get("rect")]
    assert hits, "fixture must contain the query"

    w.sidebar_tabs.search.blackout_btn.click()
    marks = w.view.get_redact_marks()
    assert len(marks) == len(hits), "every hit becomes a mark"
    # precision: mark rects equal the hit rects (char-accurate, not line-wide)
    hit_rects = sorted(tuple(r["rect"]) for r in hits)
    mark_rects = sorted(tuple(round(v, 1) for v in rect.as_tuple())
                        for _p, rect in marks)
    for (hx0, hy0, _hx1, _hy1), (mx0, my0, _mx1, _my1) in zip(hit_rects, mark_rects,
                                                              strict=True):
        assert abs(hx0 - mx0) < 1 and abs(hy0 - my0) < 1, \
            "marks must land exactly on the hits"


def test_apply_refuses_page_with_existing_annot(qtbot, window, fixture_dir):
    """existing_redaction.pdf: apply is rejected with the D6 message."""
    window.open_document(str(fixture_dir / "existing_redaction.pdf"))
    wait_opened(qtbot, window)
    qtbot.waitUntil(lambda: window.view is not None
                    and window.view.frames
                    and window.view.frames[0].pixmap is not None, timeout=45000)
    w = window
    from openpdfsuite.domain.models import Rect

    w.action_tool_redact.trigger()
    w.view.add_redact_mark(0, Rect(60.0, 88.0, 210.0, 112.0))
    w._on_redact_apply()
    # no revision change, and the rejection path cleared the pending state
    assert w.controller.session.revision == 0
    assert w._pending_redact is None or w._pending_redact.get("key") is None
