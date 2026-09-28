"""Headless Replace-All tests (M10): plan -> review filter -> apply -> save.

Drives PdfWorker.handle() directly like test_worker_headless.py. The core
guarantees under test: per-occurrence review filtering, per-page candidate
validation, ONE grouped undo entry for the whole batch, honest multi-line
save checks, and skip-with-reason for everything that cannot be replaced.
"""

from __future__ import annotations

import pymupdf
import pytest

from openpdfsuite.infrastructure.pdf import protocol as P
from openpdfsuite.infrastructure.pdf.editor import apply_ranges, find_line_matches
from openpdfsuite.infrastructure.pdf.textnorm import norm_cmp
from openpdfsuite.infrastructure.pdf.worker import PdfWorker
from tests.unit.test_worker_headless import open_standard, req


@pytest.fixture()
def worker():
    w = PdfWorker()
    yield w
    for doc_id in list(w.docs):
        try:
            w.docs[doc_id].engine.doc.close()
        except Exception:
            pass


def _two_page_doc(tmp_path):
    """Two pages, one 'alpha' line each — the multi-page batch fixture."""
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((72, 100), "alpha one", fontsize=12, fontname="helv")
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((72, 100), "alpha two", fontsize=12, fontname="helv")
    path = tmp_path / "two_page.pdf"
    doc.save(str(path))
    doc.close()
    return path


def _plan(worker, doc_id, query, replacement, **opts):
    return worker.handle(req(P.REPLACE_PLAN, {
        "query": query, "replacement": replacement,
        "match_case": opts.get("match_case", False),
        "whole_word": opts.get("whole_word", False),
        "auto_shrink": opts.get("auto_shrink", False),
    }, doc_id=doc_id))


def _apply(worker, doc_id, batch_key, include_ids):
    return worker.handle(req(P.REPLACE_APPLY, {
        "batch_key": batch_key, "include_ids": list(include_ids)}, doc_id=doc_id))


class TestMatchHelpers:
    def test_case_insensitive_default(self):
        text = "The Quick fox"
        hits = find_line_matches(text, "quick")
        assert [text[s:e] for s, e in hits] == ["Quick"]

    def test_match_case(self):
        assert find_line_matches("The Quick fox", "quick", match_case=True) == []

    def test_whole_word_blocks_substring(self):
        text = "the category cat"
        hits = find_line_matches(text, "cat", whole_word=True)
        assert [text[s:e] for s, e in hits] == ["cat"]

    def test_whole_word_with_punctuation_needle(self):
        # a needle ending in '.' must not be anchored to a word boundary
        text = "End of first section. section?"
        hits = find_line_matches(text, "section.", whole_word=True)
        assert [text[s:e] for s, e in hits] == ["section."]

    def test_multiple_occurrences_one_line(self):
        text = "cell one | cell two | cell three"
        hits = find_line_matches(text, "cell")
        assert [text[s:e] for s, e in hits] == ["cell"] * 3

    def test_apply_ranges_folds_all_occurrences(self):
        text = "cell one | cell two | cell three"
        ranges = find_line_matches(text, "cell")
        assert apply_ranges(text, ranges, "box") == "box one | box two | box three"

    def test_apply_ranges_empty_keeps_text(self):
        assert apply_ranges("abc", [], "x") == "abc"


class TestReplacePlan:
    def test_plan_finds_matches_on_standard(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = _plan(worker, doc_id, "quick", "fast")
        assert res.ok, res.error
        payload = res.payload
        assert payload["total"] == 2 and payload["ready"] == 2
        assert payload["skipped"] == 0 and not payload["truncated"]
        assert all(m["page"] == 0 for m in payload["matches"])
        assert payload["batch_key"]
        # per-page candidate with preview images for the review window
        assert len(payload["pages"]) == 1 and payload["pages"][0]["page"] == 0
        assert payload["pages"][0]["before_png"][:4] == b"\x89PNG"
        assert payload["pages"][0]["after_png"][:4] == b"\x89PNG"

    def test_plan_folds_repeated_word_per_line(self, worker, fixture_dir):
        doc_id = worker.handle(req(P.OPEN, {"path": str(fixture_dir / "tight.pdf")})).doc_id
        res = _plan(worker, doc_id, "cell", "box")
        assert res.ok
        assert res.payload["total"] == 18  # 3 per row x 6 rows
        assert res.payload["ready"] == 18

    def test_plan_whole_word_and_case(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        assert _plan(worker, doc_id, "ell", "x").payload["total"] == 0
        assert _plan(worker, doc_id, "Quick", "x", match_case=True).payload["total"] == 0
        assert _plan(worker, doc_id, "quick", "fast", whole_word=True).payload["ready"] == 2

    def test_plan_empty_query_fails(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        assert not _plan(worker, doc_id, "", "x").ok

    def test_plan_reports_unrenderable_replacement(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = _plan(worker, doc_id, "quick", "日本語")
        assert res.ok
        assert res.payload["ready"] == 0 and res.payload["skipped"] == 2
        assert all("cannot render" in m["reason"] for m in res.payload["matches"])

    def test_plan_reports_overflow_skip(self, worker, fixture_dir):
        doc_id = worker.handle(req(P.OPEN, {"path": str(fixture_dir / "tight.pdf")})).doc_id
        res = _plan(worker, doc_id, "Row", "w" * 200)
        assert res.ok
        assert res.payload["ready"] == 0 and res.payload["skipped"] == 6
        assert all("wider" in m["reason"] for m in res.payload["matches"])

    def test_plan_auto_shrink_reports_resulting_size(self, worker, fixture_dir):
        doc_id = worker.handle(req(P.OPEN, {"path": str(fixture_dir / "tight.pdf")})).doc_id
        # 120 w's ≈ 648 pt at 9 pt courier: overflows the ~519 pt line, fits at 7 pt
        res = _plan(worker, doc_id, "Row", "w" * 120, auto_shrink=True)
        assert res.ok
        assert res.payload["ready"] == 6
        sizes = {m["used_size"] for m in res.payload["matches"]}
        assert all(s is not None and s < 9.0 for s in sizes)

    def test_plan_skips_page_with_existing_redact_annot(self, worker, fixture_dir):
        doc_id = worker.handle(req(P.OPEN, {
            "path": str(fixture_dir / "existing_redaction.pdf")})).doc_id
        res = _plan(worker, doc_id, "Confidential", "Public")
        assert res.ok
        assert res.payload["ready"] == 0 and res.payload["skipped"] == 1
        assert "redaction" in res.payload["matches"][0]["reason"].lower()

    def test_plan_skips_line_that_would_become_empty(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = _plan(worker, doc_id, "End of first section.", "")
        assert res.ok
        assert res.payload["ready"] == 0 and res.payload["skipped"] == 1
        assert "Black Out" in res.payload["matches"][0]["reason"]


class TestReplaceApply:
    def test_full_cycle_multipage_one_undo(self, worker, tmp_path):
        """The headline guarantee: 2 pages, one Apply, ONE Ctrl+Z restores both."""
        worker.handle(req(P.OPEN, {"path": str(_two_page_doc(tmp_path))}))
        doc_id = next(iter(worker.docs))
        plan = _plan(worker, doc_id, "alpha", "beta")
        assert plan.ok and plan.payload["total"] == 2

        done = _apply(worker, doc_id, plan.payload["batch_key"], [0, 1])
        assert done.ok, done.error
        assert done.payload["pages"] == [0, 1]
        assert done.payload["replaced"] == 2
        assert done.payload["revision"] == 2
        # one grouped undo entry, not one per page
        assert len(worker.docs[doc_id].undo) == 1
        texts = [worker.docs[doc_id].engine.doc[i].get_text() for i in (0, 1)]
        assert "beta one" in texts[0] and "beta two" in texts[1]

        undone = worker.handle(req(P.UNDO, doc_id=doc_id))
        assert undone.ok and undone.payload["revision"] == 0
        assert undone.payload["pages"] == [0, 1]
        texts = [worker.docs[doc_id].engine.doc[i].get_text() for i in (0, 1)]
        assert "alpha one" in texts[0] and "alpha two" in texts[1]

        redone = worker.handle(req(P.REDO, doc_id=doc_id))
        assert redone.ok and redone.payload["revision"] == 2
        texts = [worker.docs[doc_id].engine.doc[i].get_text() for i in (0, 1)]
        assert "beta one" in texts[0] and "beta two" in texts[1]

    def test_apply_save_reopen_and_checks(self, worker, fixture_dir, tmp_path):
        doc_id, _ = open_standard(worker, fixture_dir)
        plan = _plan(worker, doc_id, "quick", "fast")
        done = _apply(worker, doc_id, plan.payload["batch_key"],
                      [m["id"] for m in plan.payload["matches"]])
        assert done.ok and done.payload["pages"] == [0]

        # multi-line save checks: BOTH rebuilt lines verified present and gone
        exp, forb = worker.docs[doc_id].changed_pages[0]
        assert sorted(exp) == sorted([
            "The fast brown fox jumps over the lazy dog.",
            "How vexingly fast daft zebras jump."])
        assert sorted(forb) == sorted([
            "The quick brown fox jumps over the lazy dog.",
            "How vexingly quick daft zebras jump."])

        dest = tmp_path / "saved.pdf"
        saved = worker.handle(req(P.SAVE, {"path": str(dest)}, doc_id=doc_id))
        assert saved.ok, saved.error
        check = pymupdf.open(str(dest))
        text = check[0].get_text()
        assert text.count("fast") == 2 and "quick" not in text
        check.close()

    def test_apply_respects_excluded_matches(self, worker, fixture_dir):
        doc_id = worker.handle(req(P.OPEN, {"path": str(fixture_dir / "tight.pdf")})).doc_id
        plan = _plan(worker, doc_id, "cell", "box")
        ids = [m["id"] for m in plan.payload["matches"]]
        excluded = ids[0]
        done = _apply(worker, doc_id, plan.payload["batch_key"],
                      [i for i in ids if i != excluded])
        assert done.ok
        assert done.payload["replaced"] == 17
        text = worker.docs[doc_id].engine.doc[0].get_text()
        assert "cell" in text, "the excluded occurrence must survive"
        assert "box" in text, "the included occurrences must be replaced"

    def test_apply_nothing_selected_commits_nothing(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        plan = _plan(worker, doc_id, "quick", "fast")
        done = _apply(worker, doc_id, plan.payload["batch_key"], [])
        assert done.ok and done.payload["pages"] == []
        assert worker.docs[doc_id].engine.revision == 0
        assert not worker.docs[doc_id].undo

    def test_apply_unknown_batch_key_fails(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = _apply(worker, doc_id, "ghost-key", [0])
        assert not res.ok and "expired" in res.error

    def test_apply_after_document_changed_fails(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        plan = _plan(worker, doc_id, "quick", "fast")
        # an unrelated single edit lands between plan and apply
        regions = worker.handle(req(P.EXTRACT_REGIONS, {"page": 0}, doc_id=doc_id))
        target = next(r for r in regions.payload["regions"]
                      if r.text.strip() == "Sphinx of black quartz, judge my vow.")
        from openpdfsuite.domain.models import EditMode, ReplacementEdit

        edit = ReplacementEdit(region_id=target.region_id, source_revision=0,
                               new_text="New sentence here.", mode=EditMode.PRESERVE_LINE,
                               page_index=0)
        prepared = worker.handle(req(P.PREPARE_EDIT, {"region": target, "edit": edit},
                                     doc_id=doc_id))
        assert prepared.ok, prepared.error
        committed = worker.handle(req(P.COMMIT_EDIT,
                                      {"prepare_key": prepared.payload["prepare_key"]},
                                      doc_id=doc_id))
        assert committed.ok
        res = _apply(worker, doc_id, plan.payload["batch_key"], [0, 1])
        assert not res.ok
        assert "changed" in res.error

    def test_replaced_cells_on_one_baseline_both_sides(self, worker, tmp_path):
        """Two cells on ONE baseline, needle in both: the left cell must not
        zero the right cell's width. Regression: real-world two-cells-per-row
        documents skipped every match because the left cell's origin capped
        the right cell's available space to ~0 pt."""
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "alpha one", fontsize=12, fontname="helv")
        page.insert_text((300, 100), "alpha two", fontsize=12, fontname="helv")
        path = tmp_path / "two_cells.pdf"
        doc.save(str(path))
        doc.close()
        worker.handle(req(P.OPEN, {"path": str(path)}))
        doc_id = next(iter(worker.docs))

        plan = _plan(worker, doc_id, "alpha", "beta")
        assert plan.ok
        assert plan.payload["ready"] == 2, \
            [m["reason"] for m in plan.payload["matches"]]

        done = _apply(worker, doc_id, plan.payload["batch_key"], [0, 1])
        assert done.ok and done.payload["replaced"] == 2
        text = worker.docs[doc_id].engine.doc[0].get_text()
        assert "beta one" in text and "beta two" in text

    def test_plan_matches_apply_scan_ids(self, worker, tmp_path):
        """Apply re-runs the plan scan; ids must line up for the include filter."""
        worker.handle(req(P.OPEN, {"path": str(_two_page_doc(tmp_path))}))
        doc_id = next(iter(worker.docs))
        plan = _plan(worker, doc_id, "alpha", "beta")
        ids = [m["id"] for m in plan.payload["matches"]]
        assert ids == [0, 1]  # page 0 first, then page 1
        # apply ONLY the second page's match: page 1 changes, page 0 does not
        done = _apply(worker, doc_id, plan.payload["batch_key"], [1])
        assert done.ok and done.payload["pages"] == [1]
        texts = [worker.docs[doc_id].engine.doc[i].get_text() for i in (0, 1)]
        assert "alpha one" in texts[0] and "beta two" in texts[1]


class TestReplaceAllArabic:
    """M11: logical Arabic needles match presentation-form extracted text and
    the shaped (HarfBuzz + bidi) rebuild extracts logically again."""

    def test_arabic_plan_apply_save_roundtrip(self, worker, fixture_dir, tmp_path):
        opened = worker.handle(req(P.OPEN, {"path": str(fixture_dir / "arabic.pdf")}))
        doc_id = opened.doc_id
        plan = _plan(worker, doc_id, "عربي", "إنجليزي")
        assert plan.ok, plan.error
        ready = [m for m in plan.payload["matches"] if m["status"] == "planned"]
        assert len(ready) == 1, [m["reason"] for m in plan.payload["matches"]]

        done = _apply(worker, doc_id, plan.payload["batch_key"],
                      [m["id"] for m in plan.payload["matches"]])
        assert done.ok, done.error
        assert done.payload["replaced"] == 1

        # one Ctrl+Z restores the Arabic original
        undone = worker.handle(req(P.UNDO, doc_id=doc_id))
        assert undone.ok
        text = norm_cmp(worker.docs[doc_id].engine.doc[0].get_text())
        assert norm_cmp("عربي") in text

        redone = worker.handle(req(P.REDO, doc_id=doc_id))
        assert redone.ok

        dest = tmp_path / "arabic_saved.pdf"
        saved = worker.handle(req(P.SAVE, {"path": str(dest)}, doc_id=doc_id))
        assert saved.ok, saved.error
        check = pymupdf.open(str(dest))
        text = norm_cmp(check[0].get_text())
        check.close()
        assert norm_cmp("إنجليزي") in text, "shaped replacement must extract logically"
        assert norm_cmp("عربي") not in text, "original word must be gone"

    def test_arabic_mixed_line_replace(self, worker, fixture_dir):
        """Mixed Arabic/Latin line: the needle inside the RTL run replaces and
        the untouched LTR fragment survives (bidi re-fragmentation fallback).
        Shrink-to-fit is opted in because the rebuilt run is a hair wider than
        the original fragment — the resulting size must be reported."""
        opened = worker.handle(req(P.OPEN, {"path": str(fixture_dir / "arabic.pdf")}))
        doc_id = opened.doc_id
        plan = _plan(worker, doc_id, "العلي", "سامر", auto_shrink=True)
        assert plan.ok
        ready = [m for m in plan.payload["matches"] if m["status"] == "planned"]
        assert len(ready) == 1, [m["reason"] for m in plan.payload["matches"]]
        assert ready[0]["used_size"] is not None \
            and ready[0]["used_size"] < 14.0, "shrink must be reported per match"

        done = _apply(worker, doc_id, plan.payload["batch_key"],
                      [m["id"] for m in plan.payload["matches"]])
        assert done.ok, done.error
        text = norm_cmp(worker.docs[doc_id].engine.doc[0].get_text())
        assert norm_cmp("سامر") in text
        assert norm_cmp("العلي") not in text
        assert "invoice 2026" in text, "LTR fragment must survive untouched"
        assert "Customer:" in text

    def test_arabic_longer_replacement_skipped_not_overlapped(self, worker, fixture_dir):
        """A replacement wider than the gap to the adjacent fragment is
        skipped with a reason — never silently overlapping (§9)."""
        opened = worker.handle(req(P.OPEN, {"path": str(fixture_dir / "arabic.pdf")}))
        doc_id = opened.doc_id
        plan = _plan(worker, doc_id, "العلي", "الشريف التالي")
        assert plan.ok
        assert plan.payload["ready"] == 0
        assert "wider than" in plan.payload["matches"][0]["reason"]

    def test_arabic_search_finds_logical_needle(self, worker, fixture_dir):
        """The sidebar search path shares the NFKC matching (D20)."""
        opened = worker.handle(req(P.OPEN, {"path": str(fixture_dir / "arabic.pdf")}))
        doc_id = opened.doc_id
        res = worker.handle(req(P.SEARCH, {"query": "عربي"}, doc_id=doc_id))
        assert res.ok
        assert len(res.payload["results"]) == 1
        assert res.payload["results"][0]["rect"] is not None


def test_long_document_plan_scales(worker, long_pdf):
    """120-page document: plan completes with per-page candidates and stays sane."""
    opened = worker.handle(req(P.OPEN, {"path": str(long_pdf)}))
    doc_id = opened.doc_id
    res = _plan(worker, doc_id, "Body line", "REPLACED LINE")
    assert res.ok
    # every page has one body line
    assert res.payload["total"] == 120 and res.payload["ready"] == 120
    assert len(res.payload["pages"]) == 120
    # images are capped for the review window, the candidates still exist
    with_images = sum(1 for p in res.payload["pages"] if p["before_png"])
    assert with_images == 40
