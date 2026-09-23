"""Generate the openPDF suite app icons from the brand image.

Source of truth: packaging/resources/icon_source.jpg (the brand mark, as
supplied). Outputs (all committed):
  - packaging/resources/openpdfsuite.ico       multi-size 16-256 (exe + installer)
  - packaging/resources/openpdfsuite.png       256 px (docs / installer UI)
  - src/openpdfsuite/resources/app_icon.png    256 px runtime icon (window icon,
    welcome-screen logo; bundled by the PyInstaller spec into
    _internal/openpdfsuite/resources/)

The JPG is padded to a square using its own edge colors (nothing is cropped),
then resized per size with Lanczos. Uses Pillow (a dev dependency), so the
generated files are committed and never needed at runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "packaging" / "resources" / "icon_source.jpg"
OUT_ICO = ROOT / "packaging" / "resources" / "openpdfsuite.ico"
OUT_PNG = ROOT / "packaging" / "resources" / "openpdfsuite.png"
OUT_RUNTIME = ROOT / "src" / "openpdfsuite" / "resources" / "app_icon.png"

SIZES = [16, 24, 32, 48, 64, 128, 256]


def _edge_color(im: Image.Image, side: str) -> tuple[int, int, int]:
    """Average color of one edge, used to pad that side seamlessly."""
    w, h = im.size
    strips = {
        "left": im.crop((0, 0, 1, h)),
        "right": im.crop((w - 1, 0, w, h)),
        "top": im.crop((0, 0, w, 1)),
        "bottom": im.crop((0, h - 1, w, h)),
    }
    avg = strips[side].resize((1, 1), Image.Resampling.BOX)
    return avg.getpixel((0, 0))[:3]


def square_pad(im: Image.Image) -> Image.Image:
    """Pad to square with each edge's own average color; never crops."""
    im = im.convert("RGB")
    w, h = im.size
    side = max(w, h)
    out = Image.new("RGB", (side, side))
    if w < h:
        pad = (side - w) // 2
        out.paste(Image.new("RGB", (pad, h), _edge_color(im, "left")), (0, 0))
        out.paste(Image.new("RGB", (side - w - pad, h), _edge_color(im, "right")),
                  (pad + w, 0))
        out.paste(im, (pad, 0))
    else:
        pad = (side - h) // 2
        out.paste(Image.new("RGB", (w, pad), _edge_color(im, "top")), (0, 0))
        out.paste(Image.new("RGB", (w, side - h - pad), _edge_color(im, "bottom")),
                  (0, pad + h))
        out.paste(im, (0, pad))
    return out


def main() -> int:
    if not SOURCE.exists():
        print(f"missing brand image: {SOURCE}", file=sys.stderr)
        return 1
    square = square_pad(Image.open(SOURCE))
    frames = [square.resize((s, s), Image.Resampling.LANCZOS) for s in SIZES]
    OUT_ICO.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(OUT_ICO, format="ICO", append_images=frames[:-1])
    frames[-1].save(OUT_PNG)
    OUT_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(OUT_RUNTIME)
    print(f"wrote {OUT_ICO} ({', '.join(str(s) for s in SIZES)} px)")
    print(f"wrote {OUT_PNG}")
    print(f"wrote {OUT_RUNTIME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
