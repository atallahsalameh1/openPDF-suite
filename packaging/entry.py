"""PyInstaller entry: absolute import so `openpdfsuite` stays a package.

Building src/openpdfsuite/__main__.py directly makes it a top-level `__main__`
module and breaks its relative imports; this launcher preserves them.
"""

from openpdfsuite.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
