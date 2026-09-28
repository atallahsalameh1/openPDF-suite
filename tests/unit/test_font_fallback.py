"""Glyph-aware font fallback at the engine level (M11, D20).

The user-reported class: a line set in unembedded Base-14 Times-Roman replaced
with text the Base-14 font cannot render (Base-14 has no Arabic). The resolver
must swap to an installed serif that covers the characters — the edit succeeds
with a substitution notice instead of the old hard failure. (Base-14 actually
covers precomposed Latin-1 accents; the genuine holes are Arabic/Hebrew and
combining-mark sequences.)
"""

from __future__ import annotations

import pytest

from openpdfsuite.domain.models import EditMode, ReplacementEdit
from openpdfsuite.infrastructure.pdf import protocol as P
from openpdfsuite.infrastructure.pdf.textnorm import norm_cmp
from openpdfsuite.infrastructure.pdf.worker import PdfWorker
from tests.unit.test_worker_headless import req


@pytest.fixture()
def worker():
    w = PdfWorker()
    yield w
    for doc_id in list(w.docs):
        try:
            w.docs[doc_id].engine.doc.close()
        except Exception:
            pass


def _open(worker, fixture_dir, name):
    res = worker.handle(req(P.OPEN, {"path": str(fixture_dir / name)}))
    assert res.ok, res.error
    return res.doc_id


def test_arabic_replacement_on_base14_times_succeeds(worker, fixture_dir):
    """Base-14 Times cannot render Arabic -> resolver swaps to an installed
    serif, the edit commits, and the shaped output extracts logically."""
    doc_id = _open(worker, fixture_dir, "times_roman.pdf")
    regions = worker.handle(req(P.EXTRACT_REGIONS, {"page": 0}, doc_id=doc_id))
    target = next(r for r in regions.payload["regions"]
                  if "Chloë" in r.text and r.mode == EditMode.PRESERVE_LINE)

    new_text = "Chloë met عمر in Köln."
    edit = ReplacementEdit(
        region_id=target.region_id, source_revision=0,
        new_text=new_text, mode=EditMode.PRESERVE_LINE, page_index=0)
    prepared = worker.handle(req(P.PREPARE_EDIT, {"region": target, "edit": edit},
                                 doc_id=doc_id, revision=0))
    assert prepared.ok, prepared.payload["issues"]
    validation = prepared.payload["validation"]
    assert validation.ok
    assert validation.substituted_font, "must report the substitute font"

    committed = worker.handle(req(P.COMMIT_EDIT,
                                  {"prepare_key": prepared.payload["prepare_key"]},
                                  doc_id=doc_id))
    assert committed.ok
    text = worker.docs[doc_id].engine.doc[0].get_text()
    assert norm_cmp("عمر") in norm_cmp(text)
    assert "Köln" in text
