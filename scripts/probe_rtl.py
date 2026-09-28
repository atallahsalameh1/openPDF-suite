"""M11 probe (step 0 gate): does insert_htmlbox give shaped, extractable Arabic?

Builds a candidate PDF with Arabic / mixed-bidi / CJK lines via
Page.insert_htmlbox (MuPDF Story engine: HarfBuzz shaping + bidi), then:
  1. extracts the text back and compares with the logical input
     (exact / whitespace-normalized / NFKC-normalized),
  2. renders the page so shaping can be judged visually,
  3. reports spare_height/scale from each insertion (fit semantics).

AGENTS.md §10 gate: only wire the engine path if shaping, coverage,
extraction and pixels all pass. Run:  .venv/Scripts/python scripts/probe_rtl.py
"""

from __future__ import annotations

import html
import unicodedata
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent.parent / "build" / "probe_rtl"
ARIAL = Path(r"C:\Windows\Fonts\arial.ttf")

ARABIC = "هذا نص عربي للتجربة"
MIXED = "Customer: عمر العلي — invoice 2026"
CJK = "中文测试 line"

CSS = """
@font-face { font-family: opsarabic; src: url(arial.ttf); }
body { font-family: opsarabic; font-size: 14pt; margin: 1px; }
"""


def _norm(s: str) -> str:
    return " ".join(s.split())


def _norm_nfkc(s: str) -> str:
    return _norm(unicodedata.normalize("NFKC", s))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if not ARIAL.exists():
        raise SystemExit(f"probe needs {ARIAL}")

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    arch = pymupdf.Archive()
    arch.add(str(ARIAL), "arial.ttf")

    logical = {}
    y = 80
    for name, text in (("arabic", ARABIC), ("mixed", MIXED), ("cjk", CJK)):
        rect = pymupdf.Rect(72, y, 523, y + 26)
        spare, scale = page.insert_htmlbox(
            rect, f"<div>{html.escape(text)}</div>",
            css=CSS, archive=arch, scale_low=1)
        logical[name] = text
        print(f"{name}: spare_height={spare} scale={scale}")
        y += 40

    out = OUT / "candidate.pdf"
    doc.save(str(out))
    doc.close()

    check = pymupdf.open(str(out))
    extracted = [ln for ln in check[0].get_text().splitlines() if ln.strip()]
    print("\n=== extraction (repr, ALL lines) ===")
    for line in extracted:
        print(f"   {line!r}")
        print(f"      nfkc: {_norm_nfkc(line)!r}")

    print("\n=== dict lines (what extract_page_lines sees) ===")
    data = check[0].get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for ln in block.get("lines", []):
            spans = [(s.get("text", ""), tuple(round(v, 1) for v in s["bbox"]))
                     for s in ln.get("spans", [])]
            joined = "".join(s.get("text", "") for s in ln.get("spans", []))
            print(f"   line bbox={tuple(round(v,1) for v in ln['bbox'])}")
            print(f"      joined: {_norm_nfkc(joined)!r}")
            for st, sb in spans:
                print(f"      span {sb}: {_norm_nfkc(st)!r}")

    print("\n=== codepoints, extracted arabic line vs logical ===")
    got = extracted[0] if extracted else ""
    print("got   :", [f"U+{ord(c):04X}" for c in got][:24])
    print("want  :", [f"U+{ord(c):04X}" for c in ARABIC][:24])

    pix = check[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
    png = OUT / "render.png"
    pix.save(str(png))
    print(f"\nrender -> {png} ({pix.width}x{pix.height})")
    check.close()


if __name__ == "__main__":
    main()
