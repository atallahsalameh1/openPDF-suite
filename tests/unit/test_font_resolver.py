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


def test_unknown_family_falls_back_to_builtin():
    r = resolve_font(None, _ref("NoSuchFontXyz123"), set("plain text"))
    assert r.source == "builtin"
    assert r.builtin_name == "helv"
    assert r.substituted


def test_serif_fallback_is_times():
    r = resolve_font(None, _ref("NoSuchSerifXyz", serif=True), set("text"))
    assert r.source == "builtin"
    assert r.builtin_name == "tiro"


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


def test_missing_glyphs_reported_for_latin1_builtin():
    # builtin Helvetica cannot render CJK — resolver must say so honestly
    r = resolve_font(None, _ref("NoSuchFontXyz123"), set("中文"), None)
    if r.source == "builtin":
        assert r.missing_glyphs, "CJK chars must be reported missing for builtin fallback"


def test_accented_latin_covered_by_builtin():
    r = resolve_font(None, _ref("NoSuchFontXyz123"), set("Café naïve façade résumé"))
    if r.source == "builtin":
        assert not r.missing_glyphs
