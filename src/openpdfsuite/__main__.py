"""Entry point. Guarded for Windows spawn multiprocessing and frozen builds."""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()  # required for the worker process (D4)
    if sys.platform == "win32":
        multiprocessing.set_start_method("spawn", force=True)
    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        # headless pipeline check for packaged builds (see openpdfsuite/selftest.py)
        if len(args) != 3:
            print("usage: OpenPDFSuite.exe --selftest <in.pdf> <out.pdf>",
                  file=sys.stderr)
            return 2
        from .selftest import run
        return run(args[1], args[2])
    from .app import main as app_main

    return app_main()


if __name__ == "__main__":
    raise SystemExit(main())
