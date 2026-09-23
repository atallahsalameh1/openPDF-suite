"""M5 reliability tests at the worker/engine level.

Covers: snapshot + open-from-bytes (recovery restore), the external-change
save guard, structure preservation (links / bookmarks / annotations) across
an edit+save, and encrypted-save policy.
"""

from __future__ import annotations

import itertools
import time

import pymupdf
import pytest

from openpdfsuite.domain.models import EditMode, ReplacementEdit
from openpdfsuite.infrastructure.pdf import protocol as P
from openpdfsuite.infrastructure.pdf.worker import PdfWorker
from tests.fixtures.make_fixtures import TRAILING

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


def open_doc(worker, path):
    res = worker.handle(req(P.OPEN, {"path": str(path)}))
    assert res.ok, res.error
    return res.doc_id, res.revision


def commit_line_edit(worker, doc_id, page, regions_res, new_text):
    regions = [r for r in regions_res.payload["regions"]
               if r.mode == EditMode.PRESERVE_LINE and r.text.strip() == TRAILING]
    region = regions[0]
    edit = ReplacementEdit(region_id=region.region_id, source_revision=0,
                           new_text=new_text, mode=EditMode.PRESERVE_LINE,
                           page_index=page)
    prep = worker.handle(req(P.PREPARE_EDIT, {"region": region, "edit": edit},
                             doc_id=doc_id, revision=0))
    assert prep.ok, prep.payload["issues"]
    return worker.handle(req(P.COMMIT_EDIT,
                             {"prepare_key": prep.payload["prepare_key"]},
                             doc_id=doc_id, revision=1))


def test_snapshot_and_restore(worker, fixture_dir):
    doc_id, _ = open_doc(worker, fixture_dir / "standard.pdf")
    regions = worker.handle(req(P.EXTRACT_REGIONS, {"page": 0}, doc_id=doc_id))
    commit = commit_line_edit(worker, doc_id, 0, regions, "Restored line text.")
    assert commit.ok

    snap = worker.handle(req(P.SNAPSHOT, {}, doc_id=doc_id))
    assert snap.ok and snap.payload["revision"] == 1
    data = snap.payload["data"]

    # "restart": open the recovered bytes with source metadata
    worker.handle(req(P.CLOSE, {}, doc_id=doc_id))
    res = worker.handle(req(P.OPEN, {
        "data": data, "source_path": str(fixture_dir / "standard.pdf"),
        "fingerprint": "fp", "revision": 1}))
    assert res.ok, res.error
    assert res.payload["restored"] is True
    assert res.revision == 1  # revision counter carried over
    doc2 = worker.docs[res.doc_id].engine.doc
    assert "Restored line text." in doc2[0].get_text()
    # undo history does not survive a restart — honest limitation
    undo = worker.handle(req(P.UNDO, {}, doc_id=res.doc_id))
    assert not undo.ok


def test_save_refuses_external_change(worker, fixture_dir, tmp_path):
    src = tmp_path / "ext.pdf"
    src.write_bytes((fixture_dir / "standard.pdf").read_bytes())
    doc_id, _ = open_doc(worker, src)
    regions = worker.handle(req(P.EXTRACT_REGIONS, {"page": 0}, doc_id=doc_id))
    commit_line_edit(worker, doc_id, 0, regions, "Externally guarded.")
    # another program rewrites the file after we opened it
    time.sleep(0.02)
    with open(src, "ab") as fh:
        fh.write(b"% some external trailer\n")
    saved = worker.handle(req(P.SAVE, {"path": str(src)}, doc_id=doc_id))
    assert not saved.ok
    assert "changed on disk" in saved.error
    # the on-disk file keeps the external version, not ours
    check = pymupdf.open(str(src))
    assert "Externally guarded." not in check[0].get_text()
    check.close()


def _make_annotated(src: bytes, dest) -> None:
    doc = pymupdf.open(stream=src)
    page = doc[0]
    page.insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(72, 60, 200, 82),
                      "page": 1, "to": pymupdf.Point(0, 0)})
    page.add_text_annot(pymupdf.Point(300, 60), "A note that must survive edits.")
    doc.set_toc([[1, "Chapter", 1], [2, "Section", 2]])
    doc.save(str(dest))
    doc.close()


def test_links_bookmarks_annots_survive_edit(worker, fixture_dir, tmp_path):
    src = tmp_path / "rich.pdf"
    _make_annotated((fixture_dir / "standard.pdf").read_bytes(), src)

    doc_id, _ = open_doc(worker, src)
    regions = worker.handle(req(P.EXTRACT_REGIONS, {"page": 0}, doc_id=doc_id))
    commit = commit_line_edit(worker, doc_id, 0, regions, "Preserving edit.")
    assert commit.ok
    dest = tmp_path / "rich_saved.pdf"
    saved = worker.handle(req(P.SAVE, {"path": str(dest)}, doc_id=doc_id))
    assert saved.ok, saved.error

    check = pymupdf.open(str(dest))
    text = check[0].get_text()
    assert "Preserving edit." in text and TRAILING not in text
    links = check[0].get_links()
    assert links and links[0]["kind"] == pymupdf.LINK_GOTO
    toc = check.get_toc()
    assert toc and toc[0][1] == "Chapter"
    # evaluate inside iteration: annot proxies die with their generator
    annot_types = [a.type for a in check[0].annots()]
    assert any(t[0] == pymupdf.PDF_ANNOT_TEXT for t in annot_types), \
        "unrelated annotations must survive the edit"
    check.close()


def test_encrypted_save_keeps_encryption(worker, fixture_dir, tmp_path):
    enc_src = tmp_path / "encrypted.pdf"
    doc = pymupdf.open(str(fixture_dir / "standard.pdf"))
    doc.save(str(enc_src), encryption=pymupdf.PDF_ENCRYPT_AES_256,
             user_pw="user123", owner_pw="owner123")
    doc.close()
    res = worker.handle(req(P.OPEN, {"path": str(enc_src),
                                     "password": "user123"}))
    assert res.ok, res.error
    doc_id = res.doc_id
    dest = tmp_path / "enc_saved.pdf"
    saved = worker.handle(req(P.SAVE, {"path": str(dest)}, doc_id=doc_id))
    assert saved.ok, saved.error
    reopened = pymupdf.open(str(dest))
    assert reopened.needs_pass, "output must not silently lose encryption"
    assert reopened.authenticate("user123")
    reopened.close()
