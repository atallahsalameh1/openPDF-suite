"""The --selftest entry used to verify packaged builds must stay green in dev.

Runs the real CLI (`python -m openpdfsuite --selftest …`) as a subprocess, exactly
like the packaging pipeline does on the built exe.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "generated" / "standard.pdf"


def test_selftest_cli_end_to_end(tmp_path):
    out = tmp_path / "selftest-out.pdf"
    proc = subprocess.run(
        [sys.executable, "-m", "openpdfsuite", "--selftest", str(FIXTURE), str(out)],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "SELFTEST-OK" in proc.stdout
    assert out.exists() and out.stat().st_size > 0
    log = Path(str(out) + ".selftest.log")
    assert "SELFTEST-OK" in log.read_text(encoding="utf-8")
    # the edit really happened in the output file
    import pymupdf
    doc = pymupdf.open(out)
    assert "SELFTEST" in doc[0].get_text()
    doc.close()


def test_selftest_usage_error():
    proc = subprocess.run(
        [sys.executable, "-m", "openpdfsuite", "--selftest"],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
    )
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()
