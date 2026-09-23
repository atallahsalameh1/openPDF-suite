"""Theme-aware icon provider.

Icons are 24x24 stroke SVGs (consistent 1.8px stroke, rounded caps — Fluent-like
outline style) stored as SVG text. The provider substitutes the theme's
foreground color for `currentColor`, renders at device pixel ratio, and caches.
Cache is invalidated when the theme changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICON_DIR = Path(__file__).parent.parent.parent / "resources" / "icons"

_S = 'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'

_SVGS: dict[str, str] = {
    "open": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v1H3V7Z" {_S}/><path d="M3 10h18l-2 7a2 2 0 0 1-2 1.5H7a2 2 0 0 1-2-1.5L3 10Z" {_S}/></svg>',
    "save": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M5 4h11l3 3v13H5V4Z" {_S}/><path d="M8 4v5h7V4" {_S}/><rect x="8" y="13" width="8" height="6" rx="1" {_S}/></svg>',
    "save_as": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M5 4h11l3 3v6" {_S}/><path d="M5 4v16h6" {_S}/><path d="M8 4v5h7V4" {_S}/><path d="M16.5 14.5l4 4m0-4l-4 4" {_S}/></svg>',
    "undo": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M4 9h11a5 5 0 0 1 0 10h-6" {_S}/><path d="M7.5 5.5L4 9l3.5 3.5" {_S}/></svg>',
    "redo": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M20 9H9a5 5 0 0 0 0 10h6" {_S}/><path d="M16.5 5.5L20 9l-3.5 3.5" {_S}/></svg>',
    "select": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M6 3l12 9-5.5 1.2L15.5 20 12 21.5 9.2 15 6 18V3Z" {_S}/></svg>',
    "edit_text": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M4 6V4h11v2M9.5 4v13M7 17h5" {_S}/><path d="M14.5 19.5l5-5 2 2-5 5-2.5.5.5-2.5Z" {_S}/></svg>',
    "add_text": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M4 6V4h10v2M9 4v11M6.5 15h5" {_S}/><path d="M17 13v7m-3.5-3.5h7" {_S}/></svg>',
    "zoom_in": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><circle cx="10.5" cy="10.5" r="6.5" {_S}/><path d="M15.5 15.5L21 21M10.5 7.5v6M7.5 10.5h6" {_S}/></svg>',
    "zoom_out": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><circle cx="10.5" cy="10.5" r="6.5" {_S}/><path d="M15.5 15.5L21 21M7.5 10.5h6" {_S}/></svg>',
    "fit_width": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M3 5v14M21 5v14" {_S}/><path d="M7 12h10m0 0l-2.5-2.5M17 12l-2.5 2.5M7 12l2.5-2.5M7 12l2.5 2.5" {_S}/></svg>',
    "fit_page": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><rect x="6" y="3.5" width="12" height="17" rx="1.5" {_S}/><path d="M9 8h6M9 12h6M9 16h3" {_S}/></svg>',
    "search": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><circle cx="10.5" cy="10.5" r="6.5" {_S}/><path d="M15.5 15.5L21 21" {_S}/></svg>',
    "sidebar": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><rect x="3" y="4.5" width="18" height="15" rx="2" {_S}/><path d="M9 4.5v15" {_S}/></svg>',
    "properties": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><rect x="3" y="4.5" width="18" height="15" rx="2" {_S}/><path d="M15 4.5v15" {_S}/></svg>',
    "theme_light": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="4.5" {_S}/><path d="M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6l1.5 1.5m9.8 9.8l1.5 1.5m0-12.8l-1.5 1.5M7.1 16.9l-1.5 1.5" {_S}/></svg>',
    "theme_dark": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" {_S}/></svg>',
    "close": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6L6 18" {_S}/></svg>',
    "warning": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M12 4L2.8 20h18.4L12 4Z" {_S}/><path d="M12 10v4.5" {_S}/><circle cx="12" cy="17.3" r="0.4" fill="currentColor"/></svg>',
    "info": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8.5" {_S}/><path d="M12 11v5.5" {_S}/><circle cx="12" cy="7.8" r="0.4" fill="currentColor"/></svg>',
    "chevron_left": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M14.5 5.5L8 12l6.5 6.5" {_S}/></svg>',
    "chevron_right": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M9.5 5.5L16 12l-6.5 6.5" {_S}/></svg>',
    "document": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M6 3h8l4 4v14H6V3Z" {_S}/><path d="M14 3v4h4" {_S}/><path d="M9 12h6M9 16h6" {_S}/></svg>',
    "convert": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M5 4h9l3 3v13H5V4Z" {_S}/><path d="M8 4v4h6V4" {_S}/><path d="M9.5 13.5l3 3m0-3l-3 3" {_S}/><path d="M16.5 14.5h4v4" {_S}/><path d="M20.5 14.5l-3 3 3 3" {_S}/></svg>',
    "clock": f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8.5" {_S}/><path d="M12 7.5V12l3 2" {_S}/></svg>',
    "logo": '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48" fill="none"><rect x="6" y="4" width="30" height="40" rx="4" stroke="currentColor" stroke-width="2.4"/><path d="M14 16h14M14 24h14M14 32h8" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><path d="M30 30l6 6m0-6l-6 6" stroke="#2563EB" stroke-width="2.4" stroke-linecap="round"/></svg>',
}

_cache: dict[tuple[str, str, int], QIcon] = {}

# -- brand app icon -----------------------------------------------------------

_APP_ICON_NAME = "app_icon.png"


def app_icon_path() -> Path | None:
    """Locate the brand app icon PNG for source and frozen (PyInstaller) runs."""
    here = Path(__file__).resolve().parents[2] / "resources" / _APP_ICON_NAME
    if here.exists():
        return here
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        for root in (Path(getattr(sys, "_MEIPASS", "")), exe_dir,
                     exe_dir / "_internal"):
            cand = root / "openpdfsuite" / "resources" / _APP_ICON_NAME
            if cand.exists():
                return cand
    return None


def app_icon() -> QIcon:
    """Brand icon for QApplication.setWindowIcon (null QIcon when missing)."""
    p = app_icon_path()
    return QIcon(str(p)) if p else QIcon()


def logo_pixmap(px: int = 56, radius_ratio: float = 0.16) -> QPixmap | None:
    """Brand logo as a rounded-corner pixmap at device resolution; None if absent."""
    p = app_icon_path()
    if p is None:
        return None
    pm = QPixmap(str(p))
    if pm.isNull():
        return None
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    target = max(1, int(px * dpr))
    pm = pm.scaled(target, target, Qt.AspectRatioMode.KeepAspectRatio,
                   Qt.TransformationMode.SmoothTransformation)
    r = int(target * radius_ratio)
    if r > 0:
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.0, 0.0, target, target), r, r)
        mask = QPixmap(target, target)
        mask.fill(Qt.GlobalColor.color0)
        painter = QPainter(mask)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillPath(path, Qt.GlobalColor.color1)
        painter.end()
        pm.setMask(mask.mask())
    pm.setDevicePixelRatio(dpr)
    return pm


def write_icon_files() -> None:
    """Materialize SVGs into resources/icons (for packaging/design inspection)."""
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for name, svg in _SVGS.items():
        (ICON_DIR / f"{name}.svg").write_text(svg, encoding="utf-8")


def icon(name: str, color: str, px: int = 20) -> QIcon:
    """Theme-colored icon, cached per (name, color, px)."""
    key = (name, color, px)
    if key in _cache:
        return _cache[key]
    svg = _SVGS.get(name)
    if svg is None:
        raise KeyError(f"unknown icon: {name}")
    data = svg.replace("currentColor", color).encode("utf-8")
    renderer = QSvgRenderer(data)
    pixmap = QPixmap(px, px)
    pixmap.fill()
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(1.0)
    ic = QIcon(pixmap)
    # disabled variant: Qt dims it automatically; provide a selected/active variant too
    _cache[key] = ic
    return ic


def icon_names() -> list[str]:
    return sorted(_SVGS)


def clear_cache() -> None:
    _cache.clear()
