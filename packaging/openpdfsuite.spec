# PyInstaller spec for openPDF suite (M7). Build:
#   .venv\\Scripts\\pyinstaller packaging\\openpdfsuite.spec --noconfirm
# Output: build/dist/OpenPDFSuite/OpenPDFSuite.exe (onedir; the Inno Setup script packages it).

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # project root (spec lives in packaging/)
SRC = ROOT / "src"

block_cipher = None

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # the PDF worker runs via multiprocessing spawn; it is imported
        # transitively but listed here so refactors cannot silently drop it
        "openpdfsuite.infrastructure.pdf.worker",
        # numpy is imported lazily inside editor._validate_pixels
        "numpy",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # test/dev-only tooling that must not ship
        "tkinter", "pytest", "pytestqt", "PIL", "pypdfium2",
        "unittest", "IPython",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OpenPDFSuite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX breaks PySide6 DLLs and AV-flags results; leave off
    console=False,  # GUI app; --selftest prints via attached console only
    disable_windowed_traceback=False,
    icon=str(ROOT / "packaging" / "resources" / "openpdfsuite.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="OpenPDFSuite",
)
