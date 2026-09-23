"""M3 live smoke: overlay flow against the real worker (not pytest).

Guarded like a real entry point so the spawn-based worker can boot.
"""
import multiprocessing
import sys
import time


def main() -> int:
    from openpdfsuite.app import create_app, open_window
    from openpdfsuite.domain.models import EditMode

    app = create_app(sys.argv)
    w = open_window()
    w.open_document(r"tests\fixtures\generated\standard.pdf")

    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        if w.controller and w.controller.session:
            break
    if not (w.controller and w.controller.session):
        print("SMOKE-FAIL: open timed out")
        return 1
    while not (w.view and w.view.frames and w.view.frames[0].pixmap) \
            and time.time() < deadline:
        app.processEvents()
    print("OPENED", w.controller.session.meta.page_count, "pages", flush=True)

    w.action_tool_edit.trigger()
    while not w.view.page_regions.get(0) and time.time() < deadline:
        app.processEvents()
    lines = [r for r in w.view.page_regions[0] if r.mode == EditMode.PRESERVE_LINE]
    print("REGIONS", len(lines), flush=True)
    target = next(r for r in lines if "first section" in r.text)
    w._on_region_clicked(0, target)
    print("SELECTED props:", w.properties.font_combo.currentText(),
          w.properties.size_spin.value(), flush=True)
    w._on_region_double_clicked(0, target)
    print("OVERLAY", type(w._overlay).__name__, repr(w._overlay.editor.toPlainText()),
          flush=True)
    w._overlay.cancel()
    print("CANCELLED overlay None:", w._overlay is None, flush=True)
    end = time.time() + 0.3
    while time.time() < end:
        app.processEvents()
    w.close()
    print("SMOKE-OK", flush=True)
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
