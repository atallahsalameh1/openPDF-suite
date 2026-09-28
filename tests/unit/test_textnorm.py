"""Text normalization across the extraction boundary (M11/D20)."""

from __future__ import annotations

from openpdfsuite.infrastructure.pdf.textnorm import (
    needs_bidi,
    nfkc_offset_map,
    norm_cmp,
    strip_invisibles,
)


def test_norm_cmp_folds_presentation_forms_and_nbsp():
    # shaped Arabic extracts as presentation forms + NBSP; users type logical
    assert norm_cmp("ﻫﺫﺍ\xa0ﻧﺹ") == norm_cmp("هذا نص")


def test_norm_cmp_unifies_hyphen_variants():
    # producer soft hyphens (U+00AD) and the Story engine's hyphen glyph
    # (extracts back as U+00AD) must both compare equal to a plain '-'
    assert norm_cmp("144\u00AD28.3") == norm_cmp("144-28.3")
    assert norm_cmp("a\u2010b") == "a-b"


def test_norm_cmp_strips_zero_width_invisibles():
    assert norm_cmp("Cus\u200btomer") == "Customer"
    assert strip_invisibles("a\u00ADb\u200cc") == "a-bc"


def test_norm_cmp_is_identity_for_plain_latin():
    assert norm_cmp("The quick brown fox.") == "The quick brown fox."


def test_needs_bidi_detects_arabic_and_hebrew_not_latin():
    assert needs_bidi("مرحبا")
    assert needs_bidi("שלום")
    assert needs_bidi("ﻋﻣﺭ")  # presentation forms count too
    assert not needs_bidi("Hello 144*28.3")


def test_nfkc_offset_map_maps_ranges_back_to_original():
    text = "ab\u00ADc"  # soft hyphen survives NFKC per-char
    norm, offsets = nfkc_offset_map(text)
    assert norm == "ab\u00ADc"
    assert offsets == [0, 1, 2, 3]
    # lam-alef ligature expands to two chars sharing one original index
    norm2, offsets2 = nfkc_offset_map("ﻻ")
    assert norm2 == "لا"
    assert offsets2 == [0, 0]
