"""Motion helpers (AGENTS.md §4: 120–180 ms transitions, reduced-motion aware).

Transitions are deliberately rare in openPDF suite. The one we ship — the editing
overlay fade — is gated on both the in-app Reduced Motion setting and the
Windows "show animations" system preference, so users who opted out either
place get none.
"""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPropertyAnimation,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

_SPI_GETCLIENTAREAANIMATION = 0x1042


def system_reduced_motion() -> bool:
    """True when Windows' client-area animation (animations) is disabled."""
    if sys.platform != "win32":
        return False
    try:
        value = ctypes.c_int()
        ok = ctypes.windll.user32.SystemParametersInfoW(
            _SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0)
        return bool(ok) and not bool(value.value)
    except Exception:
        return False


def reduced_motion(settings) -> bool:
    """Effective preference: explicit setting, else the OS preference."""
    return bool(getattr(settings, "reduced_motion", False)) or system_reduced_motion()


def fade_in(widget: QWidget, settings, duration_ms: int = 150) -> None:
    """Fade `widget` in over `duration_ms` unless motion is reduced.

    No-ops (returns without touching the widget) when reduced motion is
    preferred or the widget is already fading in.
    """
    if reduced_motion(settings) or widget.property("suite_fading") is True:
        return
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.finished.connect(lambda: _fade_done(widget, effect))
    widget.setProperty("suite_fading", True)
    anim.start(QAbstractAnimation.DeleteWhenStopped)


def _fade_done(widget: QWidget, effect: QGraphicsOpacityEffect) -> None:
    widget.setProperty("suite_fading", False)
    # Detach the effect so it stops interfering with rendering entirely.
    if widget.graphicsEffect() is effect:
        widget.setGraphicsEffect(None)
