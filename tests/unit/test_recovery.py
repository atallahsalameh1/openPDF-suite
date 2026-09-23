"""RecoveryStore unit tests (§11)."""

from __future__ import annotations

import os
import time

import pymupdf

from openpdfsuite.infrastructure.pdf.saver import fingerprint_file
from openpdfsuite.infrastructure.recovery import RecoveryStore


def _pdf_bytes(text: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_write_list_load_roundtrip(tmp_path):
    store = RecoveryStore(tmp_path)
    src = tmp_path / "source.pdf"
    original = _pdf_bytes("original")
    edited = _pdf_bytes("edited")
    src.write_bytes(original)
    fp = fingerprint_file(src)
    assert store.write("sess1", str(src), fp, 3, 1, edited) is not None
    entries = store.list()
    assert len(entries) == 1
    e = entries[0]
    assert e.session_id == "sess1"
    assert e.revision == 3 and e.saved_revision == 1
    assert not e.stale
    assert store.load_bytes("sess1") == edited


def test_staleness_detected(tmp_path):
    store = RecoveryStore(tmp_path)
    src = tmp_path / "source.pdf"
    edited = _pdf_bytes("edited")
    changed = _pdf_bytes("changed on disk")
    src.write_bytes(_pdf_bytes("original"))
    fp = fingerprint_file(src)
    store.write("sess1", str(src), fp, 1, 0, edited)
    os.utime(src, (time.time() + 5, time.time() + 5))  # ensure mtime moves
    src.write_bytes(changed)
    e = store.list()[0]
    assert e.stale and "changed" in e.stale_reason
    # still restorable — the bytes are independent of the source
    assert store.load_bytes("sess1") == edited


def test_missing_source_is_stale_but_kept(tmp_path):
    store = RecoveryStore(tmp_path)
    src = tmp_path / "gone.pdf"
    edited = _pdf_bytes("edited")
    src.write_bytes(_pdf_bytes("original"))
    store.write("sess1", str(src), "fp", 1, 0, edited)
    src.unlink()
    e = store.list()[0]
    assert e.stale and "no longer exists" in e.stale_reason
    assert store.load_bytes("sess1") == edited


def test_discard_removes_only_that_session(tmp_path):
    store = RecoveryStore(tmp_path)
    a, b = _pdf_bytes("a"), _pdf_bytes("b-text-longer")
    store.write("a", "", "", 1, 0, a)
    store.write("b", "", "", 2, 0, b)
    store.discard("a")
    ids = [e.session_id for e in store.list()]
    assert ids == ["b"]
    assert store.load_bytes("a") is None
    assert store.load_bytes("b") == b


def test_persistence_across_restart(tmp_path):
    data = _pdf_bytes("v2")
    RecoveryStore(tmp_path).write("sess1", "x.pdf", "fp", 2, 0, data)
    fresh = RecoveryStore(tmp_path)  # a new process would see the same dir
    entries = fresh.list()
    assert entries and entries[0].revision == 2
    assert fresh.load_bytes("sess1") == data


def test_no_temp_files_left_behind(tmp_path):
    store = RecoveryStore(tmp_path)
    store.write("sess1", "", "", 1, 0, _pdf_bytes("x"))
    leftovers = [p.name for p in store.base.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
