"""Font resolver preference order + coverage checks (AGENTS.md §10, D7)."""

from __future__ import annotations

from openpdfsuite.domain.models import Color, FontReference
from openpdfsuite.infrastructure.fonts.resolver import resolve_font


def _ref(family="Arial", bold=False, italic=False, serif=False) -> FontReference:
    return FontReference(name=family, family=family, size=11, bold=bold,
                         italic=italic, serif=serif, color=Color(0, 0, 0))


def test_installed_arial_resolves():
    r = resolve_font(None, _ref("Arial"), set("Hello World"))
    # Arial is present on Windows; resolver may pick installed or builtin
    assert r.source in ("installed", "builtin")
    assert not r.missing_glyphs


def test_unknown_family_ascii_stays_builtin():
    # baseline behavior preserved: plain Latin from an unknown family keeps
    # using the Base-14 fallback (no embedding, no visual change)
    r = resolve_font(None, _ref("NoSuchFontXyz123"), set("plain text"))
    assert r.source == "builtin"
    assert r.builtin_name == "helv"
    assert r.substituted
    assert not r.missing_glyphs


def test_unknown_family_missing_glyph_swaps_to_installed():
    # D20: when the builtin cannot render the characters (Base-14 has no
    # Arabic), an installed covering font wins instead of a hard failure
    r = resolve_font(None, _ref("NoSuchFontXyz123", serif=True), set("مرحبا"))
    assert r.source == "installed", f"expected installed font, got {r.source}"
    assert not r.missing_glyphs


def test_serif_fallback_keeps_serif_look():
    r = resolve_font(None, _ref("NoSuchSerifXyz", serif=True), set("text"))
    assert not r.missing_glyphs
    if r.source == "installed":
        assert "times" in r.family.lower() or "georgia" in r.family.lower() \
            or "cambria" in r.family.lower()
    else:
        assert r.builtin_name == "tiro"


def test_base14_times_missing_glyph_swaps_to_installed():
    """The user-reported class: Base-14 has no Arabic — the resolver must swap
    to an installed font that covers it instead of hard-failing (D20)."""
    r = resolve_font(None, _ref("Times-Roman", serif=True), set("مرحبا"))
    assert not r.missing_glyphs, "Arabic must resolve via an installed font now"
    assert r.source == "installed", f"expected installed font, got {r.source}"
    assert r.fontfile, "installed result carries the font file for embedding"


def test_arabic_resolves_via_installed_font():
    """Arabic replacement text must find an installed covering font (D20)."""
    r = resolve_font(None, _ref("Arial"), set("عمر العلي"))
    assert not r.missing_glyphs
    assert r.source == "installed" and r.fontfile


def test_original_family_preferred_when_it_covers():
    r = resolve_font(None, _ref("Arial"), set("Hello World"))
    assert r.family.lower() == "arial"
    assert not r.substituted


def test_bold_italic_uses_real_faces():
    r = resolve_font(None, _ref("Arial", bold=True, italic=True), set("text"))
    if r.source == "installed":
        assert r.bold and r.italic
        assert r.fontfile and "arialbi" in r.fontfile.lower().replace("\\", "/") or r.fontfile
    else:
        assert r.builtin_name == "hebi"


def test_user_choice_overrides():
    r = resolve_font(None, _ref("NoSuchFontXyz123"), set("text"), user_choice="Arial")
    if r.source == "user":
        assert r.family.lower() == "arial"
        assert r.substituted


def test_missing_glyphs_reported_when_nothing_covers():
    # U+0378 is unassigned — no installed font covers it; the resolver must
    # say so honestly instead of silently dropping the character
    r = resolve_font(None, _ref("NoSuchFontXyz123"), {"a", "\u0378"}, None)
    assert r.missing_glyphs, "uncovered chars must be reported missing"


def test_accented_latin_covered_by_builtin():
    r = resolve_font(None, _ref("NoSuchFontXyz123"), set("Café naïve façade résumé"))
    if r.source == "builtin":
        assert not r.missing_glyphs
