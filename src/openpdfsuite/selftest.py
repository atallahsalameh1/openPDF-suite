"""Headless self-test for packaged builds: open → region → edit → save → verify.

`OpenPDFSuite.exe --selftest <in.pdf> <out.pdf>` exercises the real worker process,
edit engine, and validated-save path without any GUI, then reopens the output
with an independent read and checks the replacement landed and the source text
left. Exit code 0 + "SELFTEST-OK" on success; 1 + "SELFTEST-FAIL: <reason>"
otherwise. This is how a clean machine without Python verifies an installed
build (AGENTS.md §15 M7).
"""

from __future__ import annotations

import sys
import time

from PySide6.QtCore import QCoreApplication

from .application.document_controller import DocumentController
from .domain.models import EditMode, ReplacementEdit


class _Steps:
    def __init__(self, app: QCoreApplication) -> None:
        self.app = app
        self.timeout = time.time() + 120

    def until(self, flag: str, obj) -> bool:
        """Pump the event loop until `obj[flag]` / `obj.flag` is truthy."""
        def value():
            return obj[flag] if isinstance(obj, dict) else getattr(obj, flag)
        while not value() and time.time() < self.timeout:
            self.app.processEvents()
        return bool(value())


_log_lines: list[str] = []


def _say(message: str) -> None:
    """Print (windowed exes have no stdout — print is a no-op) and tee to
    `<out>.selftest.log` so packaged runs leave verifiable evidence."""
    print(message, flush=True)
    _log_lines.append(message)


def _write_log(out_pdf: str) -> None:
    try:
        from pathlib import Path
        Path(out_pdf + ".selftest.log").write_text(
            "\n".join(_log_lines) + "\n", encoding="utf-8")
    except OSError:
        pass  # the exit code remains the authoritative signal


def run(in_pdf: str, out_pdf: str) -> int:
    app = QCoreApplication.instance() or QCoreApplication([])
    state = {
        "opened": False, "regions": None, "prepared": None,
        "committed": False, "saved": False, "failed": "",
    }
    c = DocumentController()

    c.document_opened.connect(lambda *a: state.__setitem__("opened", True))
    c.regions_ready.connect(
        lambda page, regions, rev: state.__setitem__("regions", regions))
    c.edit_prepared.connect(lambda info: state.__setitem__("prepared", info))
    c.edit_committed.connect(lambda *a: state.__setitem__("committed", True))
    c.saved.connect(lambda *a: state.__setitem__("saved", True))
    c.save_failed.connect(lambda msg: state.__setitem__("failed", msg))
    c.failure.connect(lambda msg: state.__setitem__("failed", msg))

    steps = _Steps(app)
    try:
        return _run_pipeline(c, state, steps, in_pdf, out_pdf)
    finally:
        c.shutdown()
        _write_log(out_pdf)


def _run_pipeline(c, state: dict, steps: _Steps, in_pdf: str,
                  out_pdf: str) -> int:
    from pathlib import Path
    Path(out_pdf).parent.mkdir(parents=True, exist_ok=True)
    c.open(in_pdf)
    if not steps.until("opened", state):
        return _fail("document never opened" + _err(state))
    if state["failed"]:
        return _fail(f"open failed: {state['failed']}")
    _say(f"selftest: opened {in_pdf}")

    c.request_regions(0)
    if not steps.until("regions", state):
        return _fail("regions never arrived" + _err(state))
    lines = [r for r in state["regions"]
             if r.mode == EditMode.PRESERVE_LINE and r.editable]
    if not lines:
        return _fail("no editable line regions on page 1")
    region = lines[0]
    original = region.text.strip()
    if not original:
        return _fail("first line region is empty")
    # a replacement strictly narrower than the source always fits (Mode A
    # collision guard) — this test proves the pipeline, not maximum fidelity
    new_text = "SELFTEST"
    edit = ReplacementEdit(
        region_id=region.region_id,
        source_revision=region.revision,
        new_text=new_text,
        mode=EditMode.PRESERVE_LINE,
    )
    _say(f"selftest: replacing {original[:40]!r} with {new_text!r}")

    state["prepared"] = None
    c.prepare_edit(0, region, edit)
    if not steps.until("prepared", state):
        return _fail("prepare never finished" + _err(state))
    info = state["prepared"]
    if not info.get("ok"):
        reasons = info.get("issues") or (
            list(info["validation"].issues) if info.get("validation") else [])
        return _fail(f"prepare rejected: {reasons}")
    _say("selftest: prepared (validated)")

    c.commit_edit(info["prepare_key"])
    if not steps.until("committed", state):
        return _fail("commit never finished" + _err(state))
    if state["failed"]:
        return _fail(f"commit failed: {state['failed']}")
    _say("selftest: committed")

    c.save(out_pdf)
    if not steps.until("saved", state):
        return _fail("save never finished" + _err(state))
    if state["failed"]:
        return _fail(f"save failed: {state['failed']}")
    # independent verification on the written file
    import pymupdf
    check = pymupdf.open(out_pdf)
    page0 = check[0].get_text()
    all_text = " ".join(page.get_text() for page in check)
    check.close()
    if "SELFTEST" not in all_text:
        return _fail("replacement text not extractable from the output")
    if original and original in page0:
        return _fail(f"source line still present on page 1: {original[:40]!r}")
    _say("selftest: reopened output — replacement present, source line gone")
    _say("SELFTEST-OK")
    return 0


def _err(state: dict) -> str:
    return f" (failure: {state['failed']})" if state["failed"] else ""


def _fail(reason: str) -> int:
    _say(f"SELFTEST-FAIL: {reason}")
    return 1


if __name__ == "__main__":  # never imported as a script in the frozen build
    raise SystemExit(run(sys.argv[1], sys.argv[2]))
