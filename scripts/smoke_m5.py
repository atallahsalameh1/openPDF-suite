"""M5 live smoke: recovery write after edit, close flush, restore on restart."""
import multiprocessing
import shutil
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    from openpdfsuite.app import create_app, open_window
    from openpdfsuite.domain.models import EditMode
    from openpdfsuite.infrastructure.recovery import RecoveryStore

    workdir = Path(tempfile.mkdtemp(prefix="openpdfsuite_m5_"))
    src = workdir / "doc.pdf"
    shutil.copy(r"tests\fixtures\generated\standard.pdf", src)

    # --- session 1: edit, then close (simulated crash-safe close) ---
    app = create_app(sys.argv)
    w = open_window()
    w.recovery = RecoveryStore(workdir / "recovery")
    w.open_document(str(src))
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        if w.controller and w.controller.session:
            break
    assert w.controller and w.controller.session
    while not (w.view and w.view.frames and w.view.frames[0].pixmap) \
            and time.time() < deadline:
        app.processEvents()
    w.action_tool_edit.trigger()
    while not w.view.page_regions.get(0) and time.time() < deadline:
        app.processEvents()
    region = next(r for r in w.view.page_regions[0]
                  if r.mode == EditMode.PRESERVE_LINE
                  and "first section" in r.text)
    w._on_region_double_clicked(0, region)
    w._confirm_preview = lambda info: True
    w._overlay.editor.setPlainText("Recovered by openPDF suite.")
    w._overlay.apply()
    while w.controller.session.revision != 1 and time.time() < deadline:
        app.processEvents()
    print("SESSION1 committed", flush=True)
    w.close()  # triggers _flush_recovery_on_close
    end = time.time() + 1.0
    while time.time() < end:
        app.processEvents()

    entries = RecoveryStore(workdir / "recovery").list()
    assert entries, "no recovery entry written on close"
    print("SESSION1 recovery entry rev", entries[0].revision, flush=True)

    # --- session 2: restart, restore, save ---
    w2 = open_window()
    w2.recovery = RecoveryStore(workdir / "recovery")
    w2._ask_recovery = lambda entry, name, note: "restore"
    w2._check_recovery()
    while not (w2.controller and w2.controller.session) and time.time() < deadline:
        app.processEvents()
    assert w2.controller and w2.controller.session
    while not (w2.view and w2.view.frames and w2.view.frames[0].pixmap) \
            and time.time() < deadline:
        app.processEvents()
    print("SESSION2 restored rev", w2.controller.session.revision,
          "dirty:", w2.controller.session.dirty, flush=True)
    dest = workdir / "saved.pdf"
    w2._on_save_as = lambda: w2.controller.save(str(dest))
    w2._saved_once = False
    w2._on_save()
    saved = {"done": False}
    while not saved["done"] and time.time() < deadline:
        app.processEvents()
        saved["done"] = dest.exists() and not w2.recovery.list()
    import pymupdf
    check = pymupdf.open(str(dest))
    ok = "Recovered by openPDF suite." in check[0].get_text()
    check.close()
    print("SESSION2 saved contains edit:", ok, flush=True)
    left = RecoveryStore(workdir / "recovery").list()
    print("SESSION2 recovery entries after save:", len(left), flush=True)
    w2.close()
    end = time.time() + 0.5
    while time.time() < end:
        app.processEvents()
    shutil.rmtree(workdir, ignore_errors=True)
    print("SMOKE-OK" if ok and not left else "SMOKE-FAIL", flush=True)
    return 0 if ok and not left else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
