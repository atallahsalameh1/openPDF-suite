"""Synthetic PDF fixtures with known content (AGENTS.md §14).

Every fixture is generated programmatically so tests can assert against known
source text and geometry. Generated files land in `tests/fixtures/generated/`
(session-scoped, cached by content of this module).

Fixture catalogue (M0 set):
  standard.pdf      — title line, a 4-line paragraph, a trailing line, page 2
  colored_bg.pdf    — paragraph on a solid blue rectangle (white text)
  unicode.pdf       — accented Latin text
  image_behind.pdf  — raster image with text drawn over it
  rotated.pdf       — page with /Rotate 90
  cropbox.pdf       — CropBox offset from MediaBox
  columns.pdf       — two text columns
  tight.pdf         — closely spaced lines (table-like)
  embedded_font.pdf — text with a real embedded TTF (subset)
  signed_like.pdf   — page carrying a pre-existing redaction annot (unsupported path)
  long.pdf          — 120 pages (navigation/perf, generated on demand)
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

FIXTURE_DIR = Path(__file__).parent / "generated"

# Known source strings tests assert against.
TITLE = "Quarterly Report 2026"
PARA_LINE1 = "The quick brown fox jumps over the lazy dog."
PARA_LINE2 = "Pack my box with five dozen liquor jugs."
PARA_LINE3 = "How vexingly quick daft zebras jump."
PARA_LINE4 = "Sphinx of black quartz, judge my vow."
TRAILING = "End of first section."
PAGE2_LINE = "Second page content stays untouched."
ACCENTED = "Café naïve façade — résumé of Zürich straße."
LEFT_COL_HEAD = "Left column heading"
RIGHT_COL_HEAD = "Right column heading"
LEFT_COL_BODY = [
    "Left column first line of text.",
    "Left column second line of text.",
    "Left column third line of text.",
]
RIGHT_COL_BODY = [
    "Right column first line of text.",
    "Right column second line of text.",
    "Right column third line of text.",
]
TIGHT_ROWS = [f"Row {i} cell one | cell two | cell three" for i in range(1, 7)]
BG_PARA = [
    "Important notice inside the panel.",
    "White text on a solid blue background.",
]


def _new_doc() -> pymupdf.Document:
    return pymupdf.open()


def make_standard(path: Path) -> None:
    doc = _new_doc()
    page = doc.new_page(width=595, height=842)  # A4-ish
    page.insert_text((72, 80), TITLE, fontsize=20, fontname="hebo")
    y = 140
    for line in (PARA_LINE1, PARA_LINE2, PARA_LINE3, PARA_LINE4):
        page.insert_text((72, y), line, fontsize=11, fontname="helv")
        y += 16
    page.insert_text((72, y + 40), TRAILING, fontsize=11, fontname="helv")
    page2 = doc.new_page(width=595, height=842)
    page2.insert_text((72, 100), PAGE2_LINE, fontsize=12, fontname="helv")
    doc.save(path)
    doc.close()


def make_colored_bg(path: Path) -> None:
    doc = _new_doc()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(pymupdf.Rect(60, 100, 520, 200), color=None, fill=(0.1, 0.2, 0.7))
    y = 130
    for line in BG_PARA:
        page.insert_text((80, y), line, fontsize=12, fontname="helv", color=(1, 1, 1))
        y += 24
    page.insert_text((72, 260), TRAILING, fontsize=11, fontname="helv")
    doc.save(path)
    doc.close()


def make_unicode(path: Path) -> None:
    doc = _new_doc()
    page = doc.new_page(width=595, height=842)
    # built-in helv covers Latin-1; use an embedded Arial for the accents test
    arial = Path("C:/Windows/Fonts/arial.ttf")
    if arial.exists():
        page.insert_text((72, 100), ACCENTED, fontsize=12, fontfile=str(arial), fontname="Arial")
    else:
        page.insert_text((72, 100), ACCENTED, fontsize=12, fontname="helv")
    doc.save(path)
    doc.close()


def make_image_behind(path: Path) -> None:
    doc = _new_doc()
    page = doc.new_page(width=595, height=842)
    # raster gradient image behind text
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100))
    for yy in range(100):
        for xx in range(0, 200, 4):
            pix.set_pixel(xx, yy, (240 - xx, 100 + yy, 200))
    page.insert_image(pymupdf.Rect(72, 120, 400, 260), pixmap=pix)
    page.insert_text((90, 180), "Text drawn over an image.", fontsize=14,
                     fontname="hebo", color=(0.9, 0.9, 0.1))
    page.insert_text((72, 320), TRAILING, fontsize=11, fontname="helv")
    doc.save(path)
    doc.close()


def make_rotated(path: Path) -> None:
    doc = _new_doc()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Rotated page headline.", fontsize=14, fontname="hebo")
    page.insert_text((72, 140), TRAILING, fontsize=11, fontname="helv")
    page.set_rotation(90)
    doc.save(path)
    doc.close()


def make_cropbox(path: Path) -> None:
    doc = _new_doc()
    page = doc.new_page(width=700, height=900)
    page.set_cropbox(pymupdf.Rect(100, 80, 650, 850))
    # coordinates are in mediabox space; crop offset (100, 80)
    page.insert_text((150, 160), "Cropbox offset headline.", fontsize=14, fontname="hebo")
    page.insert_text((150, 200), TRAILING, fontsize=11, fontname="helv")
    doc.save(path)
    doc.close()


def make_columns(path: Path) -> None:
    doc = _new_doc()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 80), LEFT_COL_HEAD, fontsize=13, fontname="hebo")
    page.insert_text((320, 80), RIGHT_COL_HEAD, fontsize=13, fontname="hebo")
    y = 110
    for left, right in zip(LEFT_COL_BODY, RIGHT_COL_BODY, strict=True):
        page.insert_text((60, y), left, fontsize=10, fontname="helv")
        page.insert_text((320, y), right, fontsize=10, fontname="helv")
        y += 15
    page.draw_line(pymupdf.Point(300, 70), pymupdf.Point(300, y + 10), color=(0.5, 0.5, 0.5))
    doc.save(path)
    doc.close()


def make_tight(path: Path) -> None:
    doc = _new_doc()
    page = doc.new_page(width=595, height=842)
    y = 100
    for row in TIGHT_ROWS:
        page.insert_text((72, y), row, fontsize=9, fontname="cour")
        y += 11  # very tight leading
    doc.save(path)
    doc.close()


def make_embedded_font(path: Path) -> None:
    doc = _new_doc()
    page = doc.new_page(width=595, height=842)
    arial = Path("C:/Windows/Fonts/arial.ttf")
    page.insert_text((72, 100), "Embedded Arial headline.", fontsize=16,
                     fontfile=str(arial) if arial.exists() else None,
                     fontname="Arial" if arial.exists() else "hebo")
    page.insert_text((72, 140), PARA_LINE1, fontsize=11,
                     fontfile=str(arial) if arial.exists() else None,
                     fontname="Arial" if arial.exists() else "helv")
    page.insert_text((72, 200), TRAILING, fontsize=11, fontname="helv")
    doc.save(path, garbage=3, deflate=True)  # force subsetting
    doc.close()


def make_existing_redaction(path: Path) -> None:
    doc = _new_doc()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Confidential draft line.", fontsize=12, fontname="helv")
    page.add_redact_annot(pymupdf.Rect(200, 300, 400, 320))  # unrelated, NOT applied
    doc.save(path)
    doc.close()


def make_long(path: Path, pages: int = 120) -> None:
    doc = _new_doc()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 80), f"Long document page {i + 1}", fontsize=16, fontname="hebo")
        page.insert_text((72, 120), f"Body line on page {i + 1}.", fontsize=11, fontname="helv")
    doc.save(path, deflate=True)
    doc.close()


BUILDERS = {
    "standard.pdf": make_standard,
    "colored_bg.pdf": make_colored_bg,
    "unicode.pdf": make_unicode,
    "image_behind.pdf": make_image_behind,
    "rotated.pdf": make_rotated,
    "cropbox.pdf": make_cropbox,
    "columns.pdf": make_columns,
    "tight.pdf": make_tight,
    "embedded_font.pdf": make_embedded_font,
    "existing_redaction.pdf": make_existing_redaction,
}


def build_all(target: Path = FIXTURE_DIR, force: bool = False) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for name, fn in BUILDERS.items():
        out = target / name
        if force or not out.exists():
            fn(out)
    return target


if __name__ == "__main__":
    build_all(force=True)
    print("fixtures written to", FIXTURE_DIR)
