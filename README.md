# openPDF suite — PDF Text Editor for Windows

openPDF suite is a local-first Windows desktop application for editing the text of
existing PDF files: select a line or paragraph, retype it, see a real
PDF-engine preview, apply, undo, and save a valid PDF. No account, no
network, no telemetry — documents never leave your machine.

## What it does

- **Open & view** — multipage PDFs via file dialog or drag-and-drop,
  thumbnails, search with match-case, zoom / fit width / fit page,
  continuous scrolling, copy text.
- **Edit existing text** — in Edit Text mode, hover shows eligible regions,
  click selects, double-click opens an in-place editor aligned to the page.
  Replace a word, a line, or a paragraph with reflow inside a resizable box.
- **Honest previews** — the before/after compare is rendered by the same
  PDF engine that writes the file, not by the input widget. Font
  substitutions and overflows are called out before anything is applied.
- **Add text** — click an empty spot in Add Text mode.
- **Undo/redo** (Ctrl+Z / Ctrl+Y), **crash recovery** of unsaved edits,
  light/dark themes, high-DPI (100–200%), full keyboard access.
- **Integrity first** — every save is written to a temp file, reopened,
  page- and text-validated, and only then atomically replaces the target.
  The original is never modified in place by an edit that failed
  validation.

## Supported editing scope (v1)

Editable today: existing horizontal text lines and conservatively grouped
paragraphs in ordinary text PDFs, with fonts resolved from the embedded
font, installed fonts, or an explicit substitute; colored backgrounds,
images, and vector art behind the text are preserved.

Not editable (the app says so instead of guessing): scanned pages without
a text layer, text converted to outlines, arbitrarily rotated/skewed text,
and pages carrying pre-existing redaction marks. Digital signatures cannot
stay valid after content changes — openPDF suite warns and asks for Save As.

## Install

1. Run `OpenPDFSuiteSetup-0.1.0.exe` (Inno Setup installer) and follow the
   wizard, or unzip the portable `OpenPDFSuite/` folder and run
   `OpenPDFSuite.exe`.
2. Windows 10/11 x64. No Python or other runtime needed.

Verify an installation headlessly (used by the packaging pipeline):

```bat
OpenPDFSuite.exe --selftest input.pdf output.pdf
```

This runs open → region → edit → save → reopen-verify against the real
engine and writes `output.pdf.selftest.log`; exit code 0 = pass.

## Build from source

Requirements: Python 3.12+, Windows 10/11 x64.

```bat
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\python tests\fixtures\make_fixtures.py   % regenerate test PDFs
.venv\Scripts\pytest                                   % 143 tests
.venv\Scripts\python -m openpdfsuite                          % run from source

:: package
.venv\Scripts\python scripts\make_icon.py
.venv\Scripts\python -m PyInstaller packaging\openpdfsuite.spec --noconfirm --distpath build\dist --workpath build\pyi
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" packaging\openpdfsuite.iss
```

Outputs:

| Artifact | Path |
|---|---|
| Standalone app folder | `build\dist\OpenPDFSuite\OpenPDFSuite.exe` |
| Installer | `build\installer\OpenPDFSuiteSetup-0.1.0.exe` |
| Icons | `packaging\resources\openpdfsuite.ico` |

### One-click Windows release build

Double-click `build.bat`, or run it from a terminal:

```bat
build.bat
```

It checks the project virtual environment and Inno Setup, builds the standalone
application folder, creates `build\portable\OpenPDFSuite-Portable-<version>.zip`,
and then builds `build\installer\OpenPDFSuiteSetup-<version>.exe`.

## License

openPDF suite's own code is proprietary to the openPDFsuite project pending the PyMuPDF
licensing decision (AGPL-3.0 or a commercial license from Artifex Software —
see THIRD_PARTY_NOTICES.md, installed with the app).
