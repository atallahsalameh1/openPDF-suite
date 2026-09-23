"""Capture openPDF suite screenshots in both themes for docs/screenshots (M6).

Renders the welcome screen, a fit-width document view, and the edit-tool
state, in light and dark themes, at an optional QT_SCALE_FACTOR (use
--scale 1.25 / 1.5 to exercise DPI layouts). Runs against a temp QSettings
store and a temp recovery store, so the machine's real settings and
%APPDATA% are never touched.

Usage:
    python scripts/make_screenshots.py [--out docs/screenshots] [--scale 1.25]
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    parser.add_argument("--scale", default="1",
                        help="QT_SCALE_FACTOR override, e.g. 1.25 or 1.5")
    parser.add_argument("--fixture", default=str(
        ROOT / "tests" / "fixtures" / "generated" / "standard.pdf"))
    args = parser.parse_args()

    import os
    os.environ.setdefault("QT_SCALE_FACTOR", args.scale)

    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtWidgets import QApplication

    # Isolate settings + recovery the same way the UI test suite does.
    tmp = Path(tempfile.mkdtemp(prefix="openpdfsuite-shots-"))
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp))
    QSettings.setPath(QSettings.IniFormat, QSettings.SystemScope, str(tmp))

    import openpdfsuite.ui.main_window as mw
    from openpdfsuite.infrastructure.recovery import RecoveryStore
    mw.RecoveryStore = lambda base=None: RecoveryStore(tmp / "recovery")

    from openpdfsuite.infrastructure.settings import Settings
    from openpdfsuite.ui.main_window import MainWindow
    from openpdfsuite.ui.themes import ThemeManager

    app = QApplication(sys.argv)
    app.setApplicationName("OpenPDFSuite")
    app.setOrganizationName("OpenPDFSuite")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    scale_tag = "" if args.scale == "1" else f"-{args.scale.replace('.', '_')}"

    def pump(ms: int) -> None:
        """Process events for roughly `ms` milliseconds."""
        end = QTimer()
        end.setSingleShot(True)
        end.start(ms)
        while end.isActive():
            app.processEvents()

    settings = Settings()
    settings.clear_recents()
    theme = ThemeManager(settings)
    w = MainWindow(settings, theme)
    w.resize(1280, 820)
    w.show()
    pump(400)

    fixture = Path(args.fixture)
    if not fixture.exists():
        print(f"fixture not found: {fixture}", file=sys.stderr)
        return 2

    for mode in ("light", "dark"):
        theme.apply(mode)
        pump(250)
        w.grab().save(str(out_dir / f"welcome-{mode}{scale_tag}.png"))

    if w.controller is None:
        w.open_document(str(fixture))
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.start(20000)
        while (w.controller is None or w.controller.session is None) \
                and deadline.isActive():
            app.processEvents()
        if w.controller is None or w.controller.session is None:
            print("document never opened", file=sys.stderr)
            return 3

    for mode in ("light", "dark"):
        theme.apply(mode)
        pump(250)
        w._fit_width()
        pump(1200)  # first page render + thumbnails
        w.grab().save(str(out_dir / f"document-{mode}{scale_tag}.png"))

        w.action_tool_edit.trigger()
        pump(900)  # region extraction for visible pages
        w.grab().save(str(out_dir / f"edit-tool-{mode}{scale_tag}.png"))
        w.action_tool_select.trigger()
        pump(150)

    # close without a recovery-flush dialog risk: nothing was edited
    w.close()
    pump(300)
    names = sorted(p.name for p in out_dir.glob(f"*{scale_tag}.png"))
    print(f"wrote {len(names)} screenshots to {out_dir}")
    for n in names:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
