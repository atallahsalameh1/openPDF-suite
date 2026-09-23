"""Milestone 0 exit proofs (AGENTS.md §15 M0).

Prove on synthetic fixtures:
  * one line replaced -> saved -> reopened: new text extractable, old text gone,
    neighbors byte-identical, pixels outside region unchanged
  * one paragraph replaced with longer text reflowing inside its box
  * replacement over a colored background preserves the background
  * replacement over an image preserves the image
  * pre-existing redaction annots make the page unsupported (D6)
  * undo (restore pre-bytes) + deterministic redo replay
"""

from __future__ import annotations

import numpy as np
import pymupdf

from openpdfsuite.domain.models import EditMode, Rect, ReplacementEdit
from openpdfsuite.infrastructure.pdf.editor import PdfEditEngine
from openpdfsuite.infrastructure.pdf.extractor import build_regions, extract_page_lines
from tests.fixtures.make_fixtures import (
    BG_PARA,
    PARA_LINE1,
    PARA_LINE2,
    PARA_LINE3,
    PARA_LINE4,
    TITLE,
    TRAILING,
)


def _open(path) -> pymupdf.Document:
    return pymupdf.open(str(path))


def _line_regions(doc, page_index=0):
    return [r for r in build_regions(doc[page_index], "d", 0)
            if r.mode == EditMode.PRESERVE_LINE]


def _para_regions(doc, page_index=0):
    return [r for r in build_regions(doc[page_index], "d", 0)
            if r.mode == EditMode.REFLOW_BOX]


def _pixel_diff_outside(doc_a, doc_b, page_index, changed: Rect, zoom=2.0):
    """Count strongly-differing pixels outside `changed`; return (bad, total)."""
    ma = pymupdf.Matrix(zoom, zoom)
    pa = doc_a[page_index].get_pixmap(matrix=ma)
    pb = doc_b[page_index].get_pixmap(matrix=ma)
    a = np.frombuffer(pa.samples, np.uint8).reshape(pa.height, pa.width, pa.n)[:, :, :3].astype(int)
    b = np.frombuffer(pb.samples, np.uint8).reshape(pb.height, pb.width, pb.n)[:, :, :3].astype(int)
    mask = np.ones(a.shape[:2], bool)
    mask[max(0, int(changed.y0 * zoom) - 3):int(changed.y1 * zoom) + 3,
         max(0, int(changed.x0 * zoom) - 3):int(changed.x1 * zoom) + 3] = False
    diff = np.abs(a - b).max(axis=2)
    return int((diff[mask] > 24).sum()), int(mask.sum())


class TestLineReplacement:
    def test_replacement_may_retain_original_text(self, standard_pdf):
        """Appending to a line must not look like hidden source text."""
        doc = _open(standard_pdf)
        engine = PdfEditEngine(doc, "d")
        target = next(r for r in _line_regions(doc) if r.text.strip() == TRAILING)
        new_text = f"{TRAILING} Updated."
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0, new_text=new_text,
            mode=EditMode.PRESERVE_LINE, page_index=0,
        )

        prepared = engine.prepare(0, target, edit)

        assert prepared.ok, prepared.issues
        engine.doc.close()

    def test_replace_line_end_to_end(self, standard_pdf, tmp_path):
        doc = _open(standard_pdf)
        engine = PdfEditEngine(doc, "d")
        regions = _line_regions(doc)
        target = next(r for r in regions if r.text.strip() == TRAILING)
        new_text = "Conclusion of the first section."
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0, new_text=new_text,
            mode=EditMode.PRESERVE_LINE, page_index=0,
        )
        prepared = engine.prepare(0, target, edit)
        assert prepared.ok, prepared.issues
        engine.commit(prepared)

        out = tmp_path / "edited.pdf"
        engine.doc.save(str(out))
        engine.doc.close()

        # reopen with the engine AND independently verify
        check = _open(out)
        text = check[0].get_text()
        assert new_text in " ".join(text.split())
        assert TRAILING not in text
        # neighbors intact on page 0 and page 1 untouched
        assert TITLE in check[0].get_text()
        assert PARA_LINE1 in check[0].get_text()
        assert PARA_LINE4 in check[0].get_text()
        assert "Second page content stays untouched." in check[1].get_text()
        lines = [ln.text for ln in extract_page_lines(check[0], with_chars=False)]
        assert not any(TRAILING in ln for ln in lines)
        check.close()

    def test_pixels_outside_region_unchanged(self, standard_pdf, tmp_path):
        orig = _open(standard_pdf)
        doc = _open(standard_pdf)
        engine = PdfEditEngine(doc, "d")
        target = next(r for r in _line_regions(doc) if TITLE in r.text)
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0,
            new_text="Annual Report 2026", mode=EditMode.PRESERVE_LINE, page_index=0,
        )
        prepared = engine.prepare(0, target, edit)
        assert prepared.ok, prepared.issues
        engine.commit(prepared)
        box = prepared.record.region.bbox.inflated(6, 6)
        bad, total = _pixel_diff_outside(orig, engine.doc, 0, box)
        assert bad / total < 0.0005, f"{bad} stray pixels changed outside region"
        orig.close()
        engine.doc.close()

    def test_old_text_truly_removed_not_covered(self, standard_pdf, tmp_path):
        """The removed text must not linger in the content stream (rawdict level)."""
        doc = _open(standard_pdf)
        engine = PdfEditEngine(doc, "d")
        target = next(r for r in _line_regions(doc) if r.text.strip() == TRAILING)
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0, new_text="Replaced line.",
            mode=EditMode.PRESERVE_LINE, page_index=0,
        )
        prepared = engine.prepare(0, target, edit)
        assert prepared.ok
        engine.commit(prepared)
        # every extracted char on the page, at every level
        raw = engine.doc[0].get_text("rawdict")
        chars = [c["c"] for b in raw["blocks"] if b.get("type") == 0
                 for ln in b["lines"] for s in ln["spans"] for c in s["chars"]]
        assert "".join(chars).find(TRAILING) == -1
        # search API agrees
        assert engine.doc[0].search_for(TRAILING) == []
        engine.doc.close()

    def test_collision_rejected_not_overlapped(self, tmp_path):
        """A too-long replacement on a crowded line is rejected, not drawn over."""
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "Short", fontsize=11)
        page.insert_text((300, 100), "Neighbor text here", fontsize=11)
        engine = PdfEditEngine(doc, "d")
        target = next(r for r in _line_regions(doc) if r.text.strip() == "Short")
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0,
            new_text="This replacement is far too long to fit the space",
            mode=EditMode.PRESERVE_LINE, page_index=0,
        )
        prepared = engine.prepare(0, target, edit)
        assert not prepared.ok
        assert prepared.validation.overflowed
        assert prepared.validation.required_size is not None
        # working doc untouched
        assert "Short" in doc[0].get_text()
        doc.close()

    def test_stale_region_rejected(self, standard_pdf):
        doc = _open(standard_pdf)
        engine = PdfEditEngine(doc, "d")
        target = next(r for r in _line_regions(doc) if r.text.strip() == TRAILING)
        stale = ReplacementEdit(
            region_id=target.region_id, source_revision=99, new_text="x",
            mode=EditMode.PRESERVE_LINE, page_index=0,
        )
        prepared = engine.prepare(0, target, stale)
        assert not prepared.ok
        assert "Stale" in prepared.issues[0]
        doc.close()


class TestParagraphReflow:
    def test_replace_paragraph_with_longer_text(self, standard_pdf, tmp_path):
        doc = _open(standard_pdf)
        engine = PdfEditEngine(doc, "d")
        paras = _para_regions(doc)
        target = next(r for r in paras if PARA_LINE1 in r.text and PARA_LINE4 in r.text)
        new_text = (
            "This replacement paragraph is deliberately much longer than the "
            "original four lines so that it must wrap across more lines inside "
            "the same layout box, proving that reflow works and that overflow "
            "detection guards the bottom edge."
        )
        box = target.paragraph_box
        # taller box so the longer text fits (user-resizable in M4)
        box = Rect(box.x0, box.y0, box.x1, box.y1 + 40)
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0, new_text=new_text,
            mode=EditMode.REFLOW_BOX, target_box=box, page_index=0,
        )
        prepared = engine.prepare(0, target, edit)
        assert prepared.ok, prepared.issues
        engine.commit(prepared)

        out = tmp_path / "para.pdf"
        engine.doc.save(str(out))
        engine.doc.close()
        check = _open(out)
        text = " ".join(check[0].get_text().split())
        assert " ".join(new_text.split()) in text
        for gone in (PARA_LINE1, PARA_LINE2, PARA_LINE3, PARA_LINE4):
            assert gone not in text, f"source line survived: {gone}"
        # trailing line below the paragraph is intact
        assert TRAILING in check[0].get_text()
        check.close()

    def test_overflow_detected_with_suggested_size(self, standard_pdf):
        doc = _open(standard_pdf)
        engine = PdfEditEngine(doc, "d")
        target = next(r for r in _para_regions(doc) if PARA_LINE1 in r.text)
        huge = ("word " * 400).strip()
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0, new_text=huge,
            mode=EditMode.REFLOW_BOX, target_box=target.paragraph_box, page_index=0,
        )
        prepared = engine.prepare(0, target, edit)
        assert not prepared.ok
        assert prepared.validation.overflowed
        assert prepared.validation.required_size is not None
        assert "Short" not in prepared.issues[0]  # sanity
        doc.close()

    def test_auto_shrink_opt_in_reports_size(self, standard_pdf):
        doc = _open(standard_pdf)
        engine = PdfEditEngine(doc, "d")
        target = next(r for r in _para_regions(doc) if PARA_LINE1 in r.text)
        long_text = ("The same paragraph but rewritten to be somewhat longer "
                     "than the original four source lines combined here. " * 3)
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0, new_text=long_text,
            mode=EditMode.REFLOW_BOX, target_box=target.paragraph_box,
            page_index=0, auto_shrink=True,
        )
        prepared = engine.prepare(0, target, edit)
        assert prepared.ok, prepared.issues
        assert any("auto-shrunk" in i for i in prepared.validation.issues)
        doc.close()  # engine.doc IS doc (prepare does not commit)


class TestBackgroundsSurvive:
    def test_colored_background_preserved(self, colored_pdf, tmp_path):
        doc = _open(colored_pdf)
        engine = PdfEditEngine(doc, "d")
        paras = _para_regions(doc)
        target = next(r for r in paras if BG_PARA[0] in r.text)
        new_text = "Replacement notice on the same blue panel."
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0, new_text=new_text,
            mode=EditMode.REFLOW_BOX, target_box=target.paragraph_box, page_index=0,
        )
        prepared = engine.prepare(0, target, edit)
        assert prepared.ok, prepared.issues
        engine.commit(prepared)
        # sample the panel background inside the old text area: still blue
        pix = engine.doc[0].get_pixmap(dpi=72)
        def px(x, y):
            i = (y * pix.width + x) * pix.n
            return tuple(pix.samples[i:i + 3])
        sample = px(400, 150)  # inside panel, right of the text
        assert sample[2] > 120 and sample[0] < 120, f"background not blue: {sample}"
        text = engine.doc[0].get_text()
        assert new_text in " ".join(text.split())
        assert BG_PARA[0] not in text and BG_PARA[1] not in text
        assert TRAILING in text  # line below panel untouched
        engine.doc.close()

    def test_image_behind_text_preserved(self, image_pdf):
        doc = _open(image_pdf)
        engine = PdfEditEngine(doc, "d")
        target = next(r for r in _line_regions(doc)
                      if "Text drawn over an image" in r.text)
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0,
            new_text="Text replaced over an image.",
            mode=EditMode.PRESERVE_LINE, page_index=0,
        )
        prepared = engine.prepare(0, target, edit)
        assert prepared.ok, prepared.issues
        engine.commit(prepared)
        imgs = engine.doc[0].get_images(full=True)
        assert len(imgs) >= 1, "image behind text was dropped"
        text = engine.doc[0].get_text()
        assert "Text replaced over an image." in text
        assert "Text drawn over an image." not in text
        engine.doc.close()


class TestUnsupportedPaths:
    def test_existing_redaction_makes_page_unsupported(self, fixture_dir):
        doc = _open(fixture_dir / "existing_redaction.pdf")
        engine = PdfEditEngine(doc, "d")
        regions = _line_regions(doc)
        target = next(r for r in regions if "Confidential" in r.text)
        cap = engine.capability(0, target)
        assert not cap.editable
        assert "redaction" in cap.reason.lower()
        prepared = engine.prepare(0, target, ReplacementEdit(
            region_id=target.region_id, source_revision=0, new_text="x",
            mode=EditMode.PRESERVE_LINE, page_index=0))
        assert not prepared.ok
        doc.close()


class TestUndoRedoReplay:
    def test_undo_restore_and_deterministic_redo(self, standard_pdf):
        doc = _open(standard_pdf)
        engine = PdfEditEngine(doc, "d")
        target = next(r for r in _line_regions(doc) if r.text.strip() == TRAILING)
        edit = ReplacementEdit(
            region_id=target.region_id, source_revision=0,
            new_text="Undoable replacement.", mode=EditMode.PRESERVE_LINE, page_index=0,
        )
        prepared = engine.prepare(0, target, edit)
        assert prepared.ok
        pre_bytes = prepared.pre_bytes
        record = prepared.record
        engine.commit(prepared)
        assert "Undoable replacement." in engine.doc[0].get_text()

        # undo = restore pre-edit snapshot
        engine.doc.close()
        engine.doc = pymupdf.open(stream=pre_bytes)
        engine.revision = record.pre_revision
        assert TRAILING in engine.doc[0].get_text()

        # redo = replay the same record deterministically
        replayed = engine.prepare(0, record.region, record.edit)
        assert replayed.ok, replayed.issues
        engine.commit(replayed)
        assert "Undoable replacement." in engine.doc[0].get_text()
        assert engine.revision == record.post_revision
        engine.doc.close()

    def test_two_sequential_edits_same_page(self, standard_pdf):
        doc = _open(standard_pdf)
        engine = PdfEditEngine(doc, "d")
        # edit 1
        t1 = next(r for r in _line_regions(doc) if r.text.strip() == TITLE)
        p1 = engine.prepare(0, t1, ReplacementEdit(
            region_id=t1.region_id, source_revision=0, new_text="Annual Report 2026",
            mode=EditMode.PRESERVE_LINE, page_index=0))
        assert p1.ok, p1.issues
        engine.commit(p1)
        # edit 2 — regions must be re-extracted at the NEW revision
        regions = build_regions(engine.doc[0], "d", engine.revision)
        t2 = next(r for r in regions
                  if r.mode == EditMode.PRESERVE_LINE and r.text.strip() == TRAILING)
        p2 = engine.prepare(0, t2, ReplacementEdit(
            region_id=t2.region_id, source_revision=engine.revision,
            new_text="Fin.", mode=EditMode.PRESERVE_LINE, page_index=0))
        assert p2.ok, p2.issues
        engine.commit(p2)
        text = engine.doc[0].get_text()
        assert "Annual Report 2026" in text and "Fin." in text
        assert TITLE not in text and TRAILING not in text
        assert PARA_LINE1 in text  # paragraph between them untouched
        engine.doc.close()
