"""Font resolution for replacement text (AGENTS.md §10, decision D7/D20).

Preference order:
  1. Reusable embedded font from the source document (coverage-checked).
  2. Matching installed Windows font (real bold/italic faces preserved).
  3. Explicitly selected substitute (user override).
  4. Coverage-matched installed fallbacks: Base-14 aliases (Times-Roman ->
     Times New Roman, Helvetica -> Arial) then stock Windows families with
     wide glyph coverage, serif-aware.
  5. Built-in Base-14 fallback — LAST resort only: in this PyMuPDF build the
     Base-14 fonts do NOT cover all of Latin-1 (Times-Roman lacks ë etc.),
     so a builtin result with `missing_glyphs` is a genuine dead end.

A matching *name* never proves usability: subset fonts may lack typed
characters, so every candidate is checked with fontTools cmap coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pymupdf

from ...domain.models import FontReference
from . import discovery

# PyMuPDF built-in fallbacks (Base-14). Last resort: NOT full Latin-1 in this
# build (verified: has_glyph('ë') is False for tiro), so anything beyond ASCII
# should have been caught by the installed fallbacks below.
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

# Base-14 / common print-family names -> installed families that keep the
# intended look (D20). Keyed on the first word of the lowercased family, since
# spans report variants like "Helvetica-Bold".
_KNOWN_ALIASES: dict[str, list[str]] = {
    "times": ["Times New Roman"],
    "helvetica": ["Arial", "Segoe UI"],
    "arial": ["Segoe UI", "Tahoma"],
    "courier": ["Courier New"],
    "georgia": ["Times New Roman"],
    "cambria": ["Times New Roman"],
    "calibri": ["Segoe UI", "Arial"],
    "segoe": ["Arial", "Tahoma"],
    "tahoma": ["Arial", "Segoe UI"],
}
# Wide-coverage stock Windows families, serif-aware order. These also carry
# Arabic/Hebrew (Arial, Segoe UI, Tahoma), which is what makes RTL replacement
# resolvable on a stock Windows install. Arial first: its metrics match what
# most producers embed, so rebuilt lines keep their original width.
_GENERIC_SANS = ["Arial", "Segoe UI", "Tahoma", "Calibri"]
_GENERIC_SERIF = ["Times New Roman", "Georgia", "Cambria"]


def _alias_families(original: FontReference) -> list[str]:
    """Installed fallback families for an original that lacks coverage (D20)."""
    key = (original.family or "").lower().split()[0] if original.family else ""
    out: list[str] = list(_KNOWN_ALIASES.get(key, []))
    for fam in (_GENERIC_SERIF if original.serif else _GENERIC_SANS):
        if fam not in out:
            out.append(fam)
    return out


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


def _try_installed(
    candidates: list[tuple[str, str]], original: FontReference,
    needed_chars: set[str], want_bold: bool, want_italic: bool,
) -> ResolvedFont | None:
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
    return None


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
    candidates: list[tuple[str, str]] = []
    if user_choice:
        candidates.append((user_choice, "user"))
    if original.family:
        candidates.append((original.family, "installed"))
    tried = {fam.lower() for fam, _ in candidates}

    resolved = _try_installed(candidates, original, needed_chars,
                              want_bold, want_italic)
    if resolved is not None:
        return resolved

    # 4. built-in fallback — keeps ordinary Latin edits byte-identical with the
    #    pre-D20 behavior (Base-14 reference fonts, no embedding)
    table = _BUILTIN_SERIF if original.serif else _BUILTIN
    name, family = table[(want_bold, want_italic)]
    missing = {c for c in needed_chars if ord(c) > 0xFF and not c.isspace()}
    try:
        f = pymupdf.Font(fontname=name)
        missing = {c for c in needed_chars if not f.has_glyph(ord(c)) and not c.isspace()}
    except Exception:
        pass
    if not missing:
        return ResolvedFont(
            family=family, source="builtin",
            substituted=family.lower() != original.family.lower(),
            builtin_name=name, bold=want_bold, italic=want_italic,
        )

    # 5. coverage fallbacks (D20): the builtin cannot render the requested
    #    characters (Times-Roman lacks ë; Base-14 has no Arabic). Try the
    #    alias/stock families that keep the intended look; only a covering
    #    font wins, otherwise report the builtin's missing glyphs honestly.
    fallback: list[tuple[str, str]] = []
    for alias in _alias_families(original):
        if alias.lower() not in tried:
            fallback.append((alias, "installed"))
            tried.add(alias.lower())
    resolved = _try_installed(fallback, original, needed_chars,
                              want_bold, want_italic)
    if resolved is not None:
        return resolved
    return ResolvedFont(
        family=family, source="builtin",
        substituted=family.lower() != original.family.lower(),
        builtin_name=name, bold=want_bold, italic=want_italic,
        missing_glyphs=sorted(missing),
    )
