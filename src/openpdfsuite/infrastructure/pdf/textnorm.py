"""Text normalization across the extraction boundary (M11, D20).

MuPDF extracts Arabic/Hebrew text as presentation-form codepoints
(U+FB1D–U+FEFF) with NBSP spaces, while users type logical characters.
NFKC folds presentation forms back to base letters, so every comparison
between user-facing text and extracted text goes through `norm_cmp`.

`needs_bidi` decides when insertion must use the shaped Story path
(HarfBuzz + bidi inside MuPDF) instead of plain insert_text.

`nfkc_offset_map` normalizes per character while tracking original indices,
so matches found in normalized space can be mapped back to exact char ranges
of the extracted string (char-accurate rects, substitutions).
"""

from __future__ import annotations

import unicodedata

# Invisible formatting characters that must never decide a comparison:
# zero-width spaces/joiners, bidi marks, word joiner, BOM. (The soft hyphen
# U+00AD is NOT here — see _HYPHEN_TABLE: producers embed it mid-word and the
# Story engine's hyphen glyph extracts back as U+00AD, so it is canonicalised
# to '-' instead of being dropped.)
_INVISIBLES = "".join(
    chr(cp) for cp in (
        0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
        0x2060, 0x2061, 0x2062, 0x2063, 0xFEFF,
    )
)
_INVISIBLE_TABLE = str.maketrans("", "", _INVISIBLES)
_HYPHEN_TABLE = str.maketrans({
    0x00AD: "-", 0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-",
})


def norm_cmp(s: str) -> str:
    """Whitespace-collapsed NFKC form with hyphen variants unified and
    invisible formatting removed.

    Used for ALL text comparisons: extraction of shaped Arabic yields
    presentation forms + NBSP while users type logical characters; producer
    files carry soft hyphens mid-word; the Story engine's hyphen glyph
    extracts back as U+00AD. All fold to the same plain text.
    """
    folded = unicodedata.normalize("NFKC", s)
    folded = folded.translate(_HYPHEN_TABLE).translate(_INVISIBLE_TABLE)
    return " ".join(folded.split())


def strip_invisibles(s: str) -> str:
    """Normalize hyphen variants and drop invisible formatting characters
    (shaped-insert markup input, D20)."""
    return s.translate(_HYPHEN_TABLE).translate(_INVISIBLE_TABLE)


def needs_bidi(s: str) -> bool:
    """True when `s` contains RTL script characters (Arabic, Hebrew, ...)."""
    for ch in s:
        cp = ord(ch)
        if (0x0590 <= cp <= 0x08FF or 0xFB1D <= cp <= 0xFDFF
                or 0xFE70 <= cp <= 0xFEFF):
            return True
        if 0x200E <= cp <= 0x200F or cp in (0x202A, 0x202B, 0x202C):
            return True  # explicit direction marks
    return False


def has_presentation_forms(s: str) -> bool:
    """True when `s` carries Arabic presentation-form codepoints (M11).

    Their presence marks text that came from EXTRACTION (a producer's shaped
    glyphs mapped back to presentation forms), possibly mixed with typed
    logical characters after a user edit.
    """
    for ch in s:
        cp = ord(ch)
        if 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF:
            return True
    return False


def _is_rtl_char(ch: str) -> bool:
    cp = ord(ch)
    return (0x0590 <= cp <= 0x08FF or 0xFB1D <= cp <= 0xFDFF
            or 0xFE70 <= cp <= 0xFEFF)


def to_logical(s: str) -> str:
    """Convert extraction-space Arabic to logical space (M11, D20).

    Producers store shaped Arabic so extraction yields the words in VISUAL
    order (left-to-right on the page = last word first) while each word's
    letters stay in logical order. The Story engine, however, expects LOGICAL
    text and applies its own bidi — feeding it visual-order text double-
    reverses the words (user-reported on Journal Entry Report (3).pdf:
    'صغير الفراولة مربى هيро' came out as 'هيرو مربى...' scrambled).

    Fix: NFKC the presentation forms to base letters, then reverse the ORDER
    OF WORDS inside each maximal RTL run (letters stay untouched — they are
    already logical). Single-word runs and Latin/digit parts are unchanged.
    """
    folded = unicodedata.normalize("NFKC", s).translate(_INVISIBLE_TABLE)
    out: list[str] = []
    run: list[str] = []  # words (and their separators) of one RTL run
    for ch in folded:
        if _is_rtl_char(ch):
            run.append(ch)
        elif ch.isspace() and run and any(_is_rtl_char(c) for c in run):
            run.append(ch)  # separator inside an RTL run
        else:
            if run:
                out.extend(_reverse_words("".join(run)))
                run = []
            out.append(ch)
    if run:
        out.extend(_reverse_words("".join(run)))
    return "".join(out)


def _reverse_words(run: str) -> str:
    words = run.split(" ")
    return " ".join(reversed(words))


def nfkc_offset_map(s: str) -> tuple[str, list[int]]:
    """NFKC-normalize `s` per character, tracking original indices.

    Returns (normalized, offsets) where offsets[i] is the index in `s` of the
    original character that produced normalized[i]. Multi-char expansions of
    one original character (e.g. the lam-alef ligature U+FEFB -> lam + alef)
    share a single index, so a range in normalized space maps back to a
    contiguous original range via (offsets[start], offsets[end - 1] + 1).
    """
    out: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(s):
        for nch in unicodedata.normalize("NFKC", ch):
            out.append(nch)
            offsets.append(i)
    return "".join(out), offsets
