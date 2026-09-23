"""Reduced-motion gating for transitions (AGENTS.md §4)."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QWidget

pytest.importorskip("pytestqt")

from openpdfsuite.ui.components.motion import (  # noqa: E402
    fade_in,
    reduced_motion,
    system_reduced_motion,
)


class _FakeSettings:
    def __init__(self, value: bool) -> None:
        self.reduced_motion = value


def test_reduced_motion_respects_explicit_setting():
    assert reduced_motion(_FakeSettings(True)) is True


def test_reduced_motion_false_when_setting_false_and_os_allows(monkeypatch):
    monkeypatch.setattr("openpdfsuite.ui.components.motion.system_reduced_motion", lambda: False)
    assert reduced_motion(_FakeSettings(False)) is False


def test_os_preference_alone_disables_motion(monkeypatch):
    monkeypatch.setattr("openpdfsuite.ui.components.motion.system_reduced_motion", lambda: True)
    assert reduced_motion(_FakeSettings(False)) is True


def test_system_probe_returns_bool():
    # never raises on any platform; on Windows reflects the OS animation flag
    assert isinstance(system_reduced_motion(), bool)


def test_fade_in_is_noop_under_reduced_motion(qtbot, monkeypatch):
    monkeypatch.setattr("openpdfsuite.ui.components.motion.system_reduced_motion", lambda: True)
    w = QWidget()
    qtbot.addWidget(w)
    w.show()
    fade_in(w, _FakeSettings(False), duration_ms=10)
    assert w.graphicsEffect() is None  # nothing attached: motion suppressed


def test_fade_in_attaches_effect_when_motion_allowed(qtbot, monkeypatch):
    monkeypatch.setattr("openpdfsuite.ui.components.motion.system_reduced_motion", lambda: False)
    w = QWidget()
    qtbot.addWidget(w)
    w.show()
    fade_in(w, _FakeSettings(False), duration_ms=120)
    assert w.graphicsEffect() is not None
    qtbot.wait(300)
    # after the fade finishes the effect is detached again
    assert w.graphicsEffect() is None
