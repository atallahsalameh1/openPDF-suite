"""Generate the openPDF suite Windows app icon (packaging/resources/openpdfsuite.ico).

Draws the app mark — a white document with a folded corner and text lines on
the accent blue, plus the blue edit stroke — at 256 px and packs a multi-size
.ico (16–256). Uses Pillow (a dev dependency), so the generated .ico is
committed and never needed at runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "packaging" / "resources" / "openpdfsuite.ico"

ACCENT = (37, 99, 235, 255)        # #2563EB
ACCENT_DEEP = (29, 85, 204, 255)   # #1D55CC
PAGE = (255, 255, 255, 255)
FOLD = (218, 226, 238, 255)        # #DAE2EE
LINE = (89, 101, 121, 255)         # #596579
EDIT = (37, 99, 235, 255)


def _rounded(draw: ImageDraw.ImageDraw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def draw_mark(size: int) -> Image.Image:
    """Render the icon at `size` px (square)."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # accent tile with generous rounding (Windows-safe squircle look)
    tile_r = max(2, int(s * 0.22))
    _rounded(d, (0, 0, s - 1, s - 1), tile_r, ACCENT)
    # subtle bottom-right depth
    _rounded(d, (int(s * 0.08), int(s * 0.08), s - 1, s - 1),
             tile_r, ACCENT)

    # document page, centered, portrait
    px0, py0 = int(s * 0.24), int(s * 0.16)
    px1, py1 = int(s * 0.76), int(s * 0.84)
    page_r = max(1, int(s * 0.04))
    _rounded(d, (px0, py0, px1, py1), page_r, PAGE)

    # folded corner (top-right of the page)
    fw = int(s * 0.12)
    fold = [(px1 - fw, py0), (px1, py0 + fw), (px1 - fw, py0 + fw)]
    d.polygon(fold, fill=FOLD)

    # text lines on the page (skip the fold area)
    if s >= 48:
        lh = max(2, int(s * 0.035))
        gap = max(2, int(s * 0.045))
        x0, x1 = int(s * 0.31), int(s * 0.69)
        y = int(s * 0.42)
        for i, width in enumerate((1.0, 1.0, 0.6)):
            yy = y + i * (lh + gap)
            if yy + lh > py1 - int(s * 0.06):
                break
            r = max(1, lh // 2)
            _rounded(d, (x0, yy, x0 + int((x1 - x0) * width), yy + lh), r, LINE)

    # edit stroke (pencil slash) at the page's lower right, accent blue
    if s >= 32:
        lw = max(2, int(s * 0.06))
        ax, ay = int(s * 0.54), int(s * 0.70)
        bx, by = int(s * 0.78), int(s * 0.94)
        d.line([(ax, by), (bx, ay)], fill=EDIT, width=lw)
        # pencil tip dot
        r = max(1, int(s * 0.035))
        _rounded(d, (bx - r, ay - r, bx + r, ay + r), r, ACCENT_DEEP)

    return img


def main() -> int:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [draw_mark(s) for s in sizes]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(OUT, format="ICO", append_images=frames[:-1])
    # also keep a 256 PNG for docs/the installer UI
    frames[-1].save(OUT.with_suffix(".png"))
    print(f"wrote {OUT} ({', '.join(str(s) for s in sizes)} px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
