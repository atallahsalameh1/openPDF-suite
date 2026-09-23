"""Headless worker tests: PdfWorker.handle() driven directly (no Qt, no process).

Covers the full request surface the UI depends on: open (incl. password),
render, thumbnail, extract, search, edit prepare/commit/undo/redo, validated
save, stale-revision rejection, close. Plus one real subprocess round-trip.
"""

from __future__ import annotations

import itertools

import pymupdf
import pytest

from openpdfsuite.domain.models import EditMode, ReplacementEdit
from openpdfsuite.infrastructure.pdf import protocol as P
from openpdfsuite.infrastructure.pdf.worker import PdfWorker, spawn_worker

_ids = itertools.count(1)


def req(kind, payload=None, doc_id=None, revision=None) -> P.Request:
    return P.Request(kind=kind, request_id=next(_ids), doc_id=doc_id,
                     revision=revision, payload=payload or {})


@pytest.fixture()
def worker():
    w = PdfWorker()
    yield w
    for doc_id in list(w.docs):
        try:
            w.docs[doc_id].engine.doc.close()
        except Exception:
            pass


def open_standard(worker, fixture_dir):
    res = worker.handle(req(P.OPEN, {"path": str(fixture_dir / "standard.pdf")}))
    assert res.ok, res.error
    assert not res.payload["needs_password"]
    return res.doc_id, res


class TestOpenClose:
    def test_open_returns_meta_and_page_sizes(self, worker, fixture_dir):
        doc_id, res = open_standard(worker, fixture_dir)
        meta = res.payload["meta"]
        assert meta.page_count == 2
        assert meta.doc_id == doc_id
        assert not meta.is_encrypted
        assert not meta.is_signed
        assert len(res.payload["page_sizes"]) == 2
        w, h = res.payload["page_sizes"][0]
        assert abs(w - 595) < 1 and abs(h - 842) < 1

    def test_open_password_protected(self, worker, tmp_path):
        path = tmp_path / "encrypted.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Encrypted content line.", fontsize=12)
        doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 user_pw="user123", owner_pw="owner123")
        doc.close()

        res = worker.handle(req(P.OPEN, {"path": str(path)}))
        assert res.ok
        assert res.payload["needs_password"]

        res = worker.handle(req(P.OPEN, {"path": str(path), "password": "wrong"}))
        assert res.payload["needs_password"]

        res = worker.handle(req(P.OPEN, {"path": str(path), "password": "user123"}))
        assert res.ok and not res.payload["needs_password"]
        assert res.payload["meta"].is_encrypted

    def test_open_missing_file_reports_error(self, worker, tmp_path):
        res = worker.handle(req(P.OPEN, {"path": str(tmp_path / "nope.pdf")}))
        assert not res.ok
        assert res.error

    def test_close_removes_doc(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = worker.handle(req(P.CLOSE, doc_id=doc_id))
        assert res.ok
        assert doc_id not in worker.docs

    def test_unknown_doc_id_fails_gracefully(self, worker):
        res = worker.handle(req(P.RENDER_PAGE, {"page": 0}, doc_id="ghost"))
        assert not res.ok
        assert "unknown document" in res.error


class TestRendering:
    def test_render_page_png(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = worker.handle(req(P.RENDER_PAGE, {"page": 0, "zoom": 1.5, "dpr": 1.0},
                                doc_id=doc_id, revision=0))
        assert res.ok, res.error
        png = res.payload["png"]
        assert png[:4] == b"\x89PNG"
        assert res.payload["width_px"] == pytest.approx(595 * 1.5, abs=2)
        assert res.payload["rotation"] == 0

    def test_render_thumbnail_smaller(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = worker.handle(req(P.RENDER_THUMBNAIL,
                                {"page": 0, "target_width": 100}, doc_id=doc_id))
        assert res.ok
        assert res.payload["width_px"] <= 105

    def test_stale_revision_rejected(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = worker.handle(req(P.RENDER_PAGE, {"page": 0, "zoom": 1.0},
                                doc_id=doc_id, revision=99))
        assert not res.ok
        assert res.error == "stale-revision"


class TestExtractionSearch:
    def test_extract_page_text(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = worker.handle(req(P.EXTRACT_PAGE_TEXT, {"page": 0}, doc_id=doc_id))
        assert res.ok
        texts = [ln[0] for ln in res.payload["lines"]]
        assert any("Quarterly Report 2026" in t for t in texts)

    def test_extract_regions(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = worker.handle(req(P.EXTRACT_REGIONS, {"page": 0}, doc_id=doc_id))
        assert res.ok
        regions = res.payload["regions"]
        assert regions
        assert all(r.revision == 0 for r in regions)
        assert any(r.mode == EditMode.REFLOW_BOX for r in regions)

    def test_search_case_insensitive(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = worker.handle(req(P.SEARCH, {"query": "quick"}, doc_id=doc_id))
        assert res.ok
        results = res.payload["results"]
        assert results and results[0]["page"] == 0
        assert results[0]["rect"] is not None

    def test_search_match_case(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        upper = worker.handle(req(P.SEARCH, {"query": "Quick", "match_case": True},
                                  doc_id=doc_id))
        lower = worker.handle(req(P.SEARCH, {"query": "quick", "match_case": True},
                                  doc_id=doc_id))
        assert upper.ok and lower.ok
        assert len(upper.payload["results"]) == 0  # source has lowercase "quick"
        assert len(lower.payload["results"]) >= 1

    def test_search_multi_page(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = worker.handle(req(P.SEARCH, {"query": "page"}, doc_id=doc_id))
        pages = {r["page"] for r in res.payload["results"]}
        assert pages == {1}  # only page 2's line contains the word "page"
        res2 = worker.handle(req(P.SEARCH, {"query": "the"}, doc_id=doc_id))
        assert {r["page"] for r in res2.payload["results"]} == {0}


class TestEditUndoRedoSave:
    def _edit_trailing(self, worker, doc_id):
        res = worker.handle(req(P.EXTRACT_REGIONS, {"page": 0}, doc_id=doc_id))
        regions = res.payload["regions"]
        target = next(r for r in regions
                      if r.mode == EditMode.PRESERVE_LINE
                      and r.text.strip() == "End of first section.")
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0,
            new_text="Conclusion of section one.", mode=EditMode.PRESERVE_LINE,
            page_index=0,
        )
        prepared = worker.handle(req(P.PREPARE_EDIT,
                                     {"region": target, "edit": edit},
                                     doc_id=doc_id, revision=0))
        assert prepared.ok, prepared.error
        assert prepared.payload["validation"].ok
        assert prepared.payload["prepare_key"]
        return prepared

    def test_full_edit_cycle(self, worker, fixture_dir, tmp_path):
        doc_id, _ = open_standard(worker, fixture_dir)
        prepared = self._edit_trailing(worker, doc_id)

        committed = worker.handle(req(P.COMMIT_EDIT,
                                      {"prepare_key": prepared.payload["prepare_key"]},
                                      doc_id=doc_id, revision=0))
        assert committed.ok, committed.error
        assert committed.payload["revision"] == 1
        assert committed.payload["can_undo"] and not committed.payload["can_redo"]

        # document now contains new text, not old
        text = worker.docs[doc_id].engine.doc[0].get_text()
        assert "Conclusion of section one." in text
        assert "End of first section." not in text

        # undo restores
        undone = worker.handle(req(P.UNDO, doc_id=doc_id))
        assert undone.ok and undone.payload["revision"] == 0
        text = worker.docs[doc_id].engine.doc[0].get_text()
        assert "End of first section." in text
        assert undone.payload["can_redo"]

        # redo replays
        redone = worker.handle(req(P.REDO, doc_id=doc_id))
        assert redone.ok and redone.payload["revision"] == 1
        text = worker.docs[doc_id].engine.doc[0].get_text()
        assert "Conclusion of section one." in text

        # validated save
        dest = tmp_path / "saved.pdf"
        saved = worker.handle(req(P.SAVE, {"path": str(dest)}, doc_id=doc_id,
                                revision=1))
        assert saved.ok, saved.error
        check = pymupdf.open(str(dest))
        text = check[0].get_text()
        assert "Conclusion of section one." in text
        assert "End of first section." not in text
        check.close()
        # source fixture untouched
        orig = pymupdf.open(str(fixture_dir / "standard.pdf"))
        assert "End of first section." in orig[0].get_text()
        orig.close()

    def test_prepare_overflow_not_committable(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = worker.handle(req(P.EXTRACT_REGIONS, {"page": 0}, doc_id=doc_id))
        target = next(r for r in res.payload["regions"]
                      if r.mode == EditMode.REFLOW_BOX)
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0,
            new_text="word " * 500, mode=EditMode.REFLOW_BOX,
            target_box=target.paragraph_box, page_index=0,
        )
        prepared = worker.handle(req(P.PREPARE_EDIT, {"region": target, "edit": edit},
                                     doc_id=doc_id, revision=0))
        assert not prepared.ok
        assert prepared.payload["validation"].overflowed
        # nothing was committed
        assert worker.docs[doc_id].engine.revision == 0

    def test_commit_expired_key_fails(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = worker.handle(req(P.COMMIT_EDIT, {"prepare_key": "nosuchkey"},
                                doc_id=doc_id))
        assert not res.ok
        assert "expired" in res.error

    def test_stale_region_prepare_rejected(self, worker, fixture_dir):
        doc_id, _ = open_standard(worker, fixture_dir)
        res = worker.handle(req(P.EXTRACT_REGIONS, {"page": 0}, doc_id=doc_id))
        target = res.payload["regions"][0]
        stale_target = ReplacementEdit(
            region_id=target.region_id, source_revision=7, new_text="x",
            mode=EditMode.PRESERVE_LINE, page_index=0,
        )
        prepared = worker.handle(req(P.PREPARE_EDIT,
                                     {"region": target, "edit": stale_target},
                                     doc_id=doc_id, revision=0))
        assert not prepared.ok
        assert "Stale" in (prepared.error or "")


class TestSubprocessRoundtrip:
    def test_spawn_send_receive_shutdown(self, fixture_dir):
        proc, conn = spawn_worker()
        try:
            conn.send(req(P.OPEN, {"path": str(fixture_dir / "standard.pdf")}))
            assert conn.poll(30), "worker did not respond"
            res = conn.recv()
            assert res.ok and res.kind == P.OPEN
            doc_id = res.doc_id
            conn.send(req(P.RENDER_PAGE, {"page": 0, "zoom": 1.0, "dpr": 1.0},
                          doc_id=doc_id, revision=0))
            assert conn.poll(30)
            res = conn.recv()
            assert res.ok and res.payload["png"][:4] == b"\x89PNG"
        finally:
            conn.send(req(P.SHUTDOWN))
            proc.join(10)
            conn.close()
        assert not proc.is_alive()
