"""Font resolution for replacement text (AGENTS.md §10, decision D7).

Preference order:
  1. Reusable embedded font from the source document (coverage-checked).
  2. Matching installed Windows font (real bold/italic faces preserved).
  3. Explicitly selected substitute (user override).
  4. Built-in fallback (PyMuPDF Base-14 Helvetica/Times — license-clean).

A matching *name* never proves usability: subset fonts may lack typed
characters, so every candidate is checked with fontTools cmap coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pymupdf

from ...domain.models import FontReference
from . import discovery

# PyMuPDF built-in fallbacks (Base-14). Always available, Latin-1 coverage.
_BUILTIN = {
    (False, False): ("helv", "Helvetica"),
    (True, False): ("hebo", "Helvetica-Bold"),
    (False, True): ("heit", "Helvetica-Oblique"),
    (True, True): ("hebi", "Helvetica-BoldOblique"),
}
_BUILTIN_SERIF = {
    (False, False): ("tiro", "Times-Roman"),
    (True, False): ("tibo", "Times-Bold"),
    (False, True): ("tiit", "Times-Italic"),
    (True, True): ("tibi", "Times-BoldItalic"),
}


@dataclass
class ResolvedFont:
    """How to draw replacement text."""

    family: str  # display family name
    source: str  # "embedded" | "installed" | "user" | "builtin"
    substituted: bool  # True when family differs from the original
    # exactly one of these is set:
    fontfile: str | None = None  # path to installed font file
    fontbuffer: bytes | None = None  # extracted embedded font program
    builtin_name: str | None = None  # pymupdf base-14 name ("helv"...)
    missing_glyphs: list[str] = field(default_factory=list)
    bold: bool = False
    italic: bool = False

    @property
    def needs_unicode_encoding(self) -> bool:
        return self.fontfile is not None or self.fontbuffer is not None


def _extract_embedded(doc: pymupdf.Document, xref: int) -> bytes | None:
    try:
        _name, _ext, _ftype, buffer = doc.extract_font(xref)
    except Exception:
        return None
    return buffer if buffer and len(buffer) > 0 else None


def resolve_font(
    doc: pymupdf.Document | None,
    original: FontReference,
    needed_chars: set[str],
    bold: bool | None = None,
    italic: bool | None = None,
    user_choice: str | None = None,
) -> ResolvedFont:
    """Resolve a font able to render `needed_chars`."""
    want_bold = original.bold if bold is None else bold
    want_italic = original.italic if italic is None else italic

    # 1. embedded reuse — only for the same requested weight (subset fonts of a
    #    different face would fake bold/italic)
    if doc is not None and original.embedded_xref > 0 and bold is None and italic is None:
        buf = _extract_embedded(doc, original.embedded_xref)
        if buf:
            missing = discovery.coverage_of_buffer(buf, needed_chars)
            if not missing:
                return ResolvedFont(
                    family=original.family, source="embedded", substituted=False,
                    fontbuffer=buf, bold=want_bold, italic=want_italic,
                )

    # 3-before-2: explicit user choice wins over automatic matching
    candidates: list[tuple[str, str]] = []  # (family, source)
    if user_choice:
        candidates.append((user_choice, "user"))
    candidates.append((original.family, "installed"))

    for family, source in candidates:
        fam = discovery.find_family(family)
        if not fam:
            continue
        path = fam.get(want_bold, want_italic)
        if not path:
            continue
        missing = discovery.coverage_of_file(path, needed_chars)
        if not missing:
            return ResolvedFont(
                family=fam.family, source=source,
                substituted=(source == "user") or fam.family.lower() != original.family.lower(),
                fontfile=path, bold=want_bold, italic=want_italic,
            )
        # installed font found but lacks glyphs — keep looking at other candidates
        continue

    # 4. built-in fallback (Latin-1). Report missing glyphs honestly.
    table = _BUILTIN_SERIF if original.serif else _BUILTIN
    name, family = table[(want_bold, want_italic)]
    missing = {c for c in needed_chars if ord(c) > 0xFF and not c.isspace()}
    try:
        f = pymupdf.Font(fontname=name)
        missing = {c for c in needed_chars if not f.has_glyph(ord(c)) and not c.isspace()}
    except Exception:
        pass
    return ResolvedFont(
        family=family, source="builtin",
        substituted=family.lower() != original.family.lower(),
        builtin_name=name, bold=want_bold, italic=want_italic,
        missing_glyphs=sorted(missing),
    )
