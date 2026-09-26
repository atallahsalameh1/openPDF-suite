# openPDF suite

A local-first Windows desktop editor for the text of existing PDF files.

openPDF suite opens a PDF, lets you click a line or paragraph and retype it, shows a preview rendered by the real PDF engine, and saves a clean, valid PDF. There is no account, no network, and no telemetry — documents never leave your machine.

## Features

- **Edit existing text** — hover highlights editable regions, a click selects, a double-click opens an in-place editor aligned to the page. Replace a word, a line, or a paragraph with reflow inside a resizable box. Fonts are resolved from the embedded font, installed fonts, or an explicit substitute, with a visible notice whenever a substitution happens.
- **Honest previews** — the before/after compare is rendered by the same PDF engine that writes the file, so what you approve is what you get. Font substitutions and text overflow are reported before anything is applied; nothing shrinks silently.
- **Convert to Word** — export the PDF as an editable `.docx` with re-merged paragraphs, inferred alignment and spacing, real Word tables from ruled grids, and inline images at their true size.
- **Add text** — click an empty spot and type, with the same font, preview, and validation pipeline as replacement.
- **Integrity first** — every save is written to a temporary file, reopened, validated page-by-page (text re-extraction plus pixel comparison), and only then atomically swapped into place. A failed edit never touches the original.
- **A real Windows app** — multipage viewing with thumbnails and search, zoom and fit modes, undo/redo, crash recovery of unsaved edits, light and dark themes, high-DPI support (100–200%), full keyboard access.

## What it does not do

openPDF suite is deliberate about scope. Scanned pages (no text layer), text converted to vector outlines, and pages carrying pre-existing redaction marks are viewable but reported as not editable instead of guessed at. Digital signatures cannot remain valid after content changes — the app warns and directs you to Save As.

## Install

1. Grab the latest installer or portable ZIP from [Releases](../../releases).
2. Run `OpenPDFSuiteSetup-<version>.exe`, or unzip the portable folder and start `OpenPDFSuite.exe`.
3. Windows 10/11 x64. No Python or other runtime required.

To verify an installation headlessly:

```bat
OpenPDFSuite.exe --selftest input.pdf output.pdf
```

This runs open → select → edit → save → reopen-verify against the real engine and writes `output.pdf.selftest.log`. Exit code 0 means pass.

## Build from source

Requirements: Windows 10/11 x64 and Python 3.12+.

```bat
git clone https://github.com/atallahsalameh1/openPDF-suite.git
cd openPDF-suite

python -m venv .venv
.venv\Scripts\pip install -e .[dev]

:: regenerate the synthetic test PDFs
.venv\Scripts\python tests\fixtures\make_fixtures.py

:: run the test suite
.venv\Scripts\python -m pytest

:: run the app from source
.venv\Scripts\python -m openpdfsuite
```

### Packaging

```bat
:: standalone app folder
.venv\Scripts\python scripts\make_icon.py
.venv\Scripts\python -m PyInstaller packaging\openpdfsuite.spec --noconfirm

:: installer (requires Inno Setup 6)
iscc packaging\openpdfsuite.iss
```

| Artifact | Path |
|---|---|
| Standalone app folder | `build\dist\OpenPDFSuite\OpenPDFSuite.exe` |
| Installer | `build\installer\OpenPDFSuiteSetup-<version>.exe` |

## Architecture

The codebase separates presentation (Qt widgets, theming), application (sessions, undo/redo, commands), domain (text regions, coordinates, validation models), and infrastructure (a dedicated worker process that owns all PyMuPDF access). Document mutation never happens in widget event handlers, and the UI thread never performs PDF work.

## License

The openPDF suite source code is proprietary, pending the PyMuPDF licensing decision (AGPL-3.0 or a commercial license from Artifex Software). Dependency licenses and redistribution terms are catalogued in the third-party notices document distributed with the application.
