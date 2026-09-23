"""Installed-font discovery on Windows (no Qt dependency — runs in the worker).

Scans `%WINDIR%/Fonts` lazily and builds family -> face-file maps using
fontTools name tables. Results are cached per process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

FACES = ("regular", "bold", "italic", "bold_italic")


@dataclass
class FamilyFiles:
    """Face file paths for one family."""

    family: str
    files: dict[str, str] = field(default_factory=dict)  # face -> path

    def get(self, bold: bool, italic: bool) -> str | None:
        key = ("bold" if bold else "") + ("_italic" if italic else "")
        key = key.strip("_") or "regular"
        # fall back: bold_italic -> bold -> regular, etc.
        for cand in (key, "bold" if bold else key, "regular"):
            if cand in self.files:
                return self.files[cand]
        return next(iter(self.files.values()), None)


_cache: dict[str, FamilyFiles] | None = None


def _font_dirs() -> list[Path]:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    dirs = [Path(windir) / "Fonts"]
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
    if local.exists():
        dirs.append(local)
    return dirs


def _classify(family: str, subfamily: str, ps_name: str) -> str:
    s = (subfamily + " " + ps_name).lower()
    bold = "bold" in s or "black" in s or "semibold" in s
    italic = "italic" in s or "oblique" in s
    if bold and italic:
        return "bold_italic"
    if bold:
        return "bold"
    if italic:
        return "italic"
    return "regular"


def _scan() -> dict[str, FamilyFiles]:
    from fontTools.ttLib import TTCollection, TTFont

    families: dict[str, FamilyFiles] = {}

    def add(path: str, font_number: int | None):
        try:
            tt = TTFont(path, fontNumber=font_number or 0, lazy=True)
            name = tt["name"]
            def nm(nid: int) -> str:
                rec = name.getName(nid, 3, 1, 0x409) or name.getName(nid, 1, 0, 0)
                return str(rec) if rec else ""
            family = nm(1) or nm(16)
            subfamily = nm(2) or nm(17)
            ps = nm(6)
            tt.close()
        except Exception:
            return
        if not family:
            return
        face = _classify(family, subfamily, ps)
        entry = families.setdefault(family, FamilyFiles(family=family))
        # prefer the first-seen file for a face; prefer ttf over ttc for reuse
        if face not in entry.files or (Path(entry.files[face]).suffix.lower() == ".ttc"
                                       and Path(path).suffix.lower() == ".ttf"):
            entry.files[face] = path

    for d in _font_dirs():
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            suf = p.suffix.lower()
            if suf in (".ttf", ".otf"):
                add(str(p), 0)
            elif suf == ".ttc":
                try:
                    col = TTCollection(str(p), lazy=True)
                    n = len(col.fonts)
                    col.close()
                except Exception:
                    continue
                for i in range(n):
                    add(str(p), i)
    return families


def installed_families() -> dict[str, FamilyFiles]:
    global _cache
    if _cache is None:
        _cache = _scan()
    return _cache


def find_family(family: str) -> FamilyFiles | None:
    fams = installed_families()
    if family in fams:
        return fams[family]
    low = family.lower().replace("-", " ")
    for key, val in fams.items():
        if key.lower().replace("-", " ") == low:
            return val
    # loose: match on the first word ("Arial Narrow" query matches "Arial")
    first = low.split()[0] if low else ""
    if first:
        for key, val in fams.items():
            if key.lower().split()[0] == first:
                return val
    return None


def coverage_of_file(path: str, chars: set[str]) -> set[str]:
    """Return the subset of `chars` NOT covered by the font file's cmap."""
    from fontTools.ttLib import TTFont

    try:
        tt = TTFont(path, fontNumber=0, lazy=True)
        cmap = tt.getBestCmap()
        tt.close()
    except Exception:
        return set(chars)
    return {c for c in chars if ord(c) not in cmap and not c.isspace()}


def coverage_of_buffer(buf: bytes, chars: set[str]) -> set[str]:
    from io import BytesIO

    from fontTools.ttLib import TTFont

    try:
        tt = TTFont(BytesIO(buf), fontNumber=0, lazy=True)
        cmap = tt.getBestCmap()
        tt.close()
    except Exception:
        return set(chars)
    return {c for c in chars if ord(c) not in cmap and not c.isspace()}
