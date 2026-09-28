"""Dedicated PDF worker process (AGENTS.md §7, decision D4).

One process, serial request handling, owns every PyMuPDF document instance.
The UI talks to it through a multiprocessing pipe using `protocol.Request` /
`protocol.Result`. Nothing here imports Qt.

Undo/redo model (worker side): byte snapshots. Each commit pushes
(pre_bytes, record) onto the undo stack; undo/redo swap documents by reopening
snapshots — deterministic and independent of extraction indices. The stack is
capped; recovery replay (M5) uses the stored EditRecords.
"""

from __future__ import annotations

import itertools
import multiprocessing
import os
import traceback
import uuid
from dataclasses import dataclass, field

import pymupdf

from ...domain.models import (
    DocumentMeta,
    DocxExportOptions,
    EditMode,
    Rect,
    ReplacementEdit,
    TextRegion,
)
from ..docx.docx_writer import DocxExportError, write_docx_atomic
from .editor import (
    BatchPair,
    EditRecord,
    PairOutcome,
    PdfEditEngine,
    PreparedEdit,
    apply_ranges,
    find_line_matches,
)
from .extractor import build_regions, extract_page_lines, page_geometry
from .protocol import (
    SHUTDOWN,
    Request,
    Result,
)
from .saver import SaveChecks, SaveError, fingerprint_file, has_signature, save_validated
from .textnorm import norm_cmp

UNDO_CAP = 32
MAX_SEARCH_RESULTS = 500
MAX_PREVIEW_PAGES = 40  # before/after images shipped to the review window (M10)
MAX_BATCH_PLANS = 3  # remembered Replace-All plans per document


def file_fingerprint(path: str) -> str:
    return fingerprint_file(path)


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


@dataclass
class _BatchPlan:
    """A planned Replace-All run (M10), kept worker-side until applied.

    Matches carry their ids from the plan scan; apply re-runs the identical
    deterministic scan (the revision guard guarantees the same text) and keeps
    only the included ids, so the review window's checkboxes drive apply
    without shipping regions across the pipe twice.
    """

    key: str
    revision: int
    query: str
    replacement: str
    match_case: bool
    whole_word: bool
    auto_shrink: bool
    matches: dict[int, dict] = field(default_factory=dict)
    prepared: dict[int, PreparedEdit] = field(default_factory=dict)  # page -> plan candidate
    page_rects: dict[int, tuple] = field(default_factory=dict)  # page -> display-space change rect
    preview_zoom: float = 2.0


def _match_page_regions(
    engine: PdfEditEngine, doc_id: str, page_index: int, query: str,
    match_case: bool, whole_word: bool, counter, include: set[int] | None = None,
) -> tuple[list[tuple[TextRegion, list[tuple[int, int]], list[int]]], bool]:
    """Deterministic match scan for one page (M10).

    Assigns match ids in a fixed order — pages ascending, lines in extraction
    order, occurrences left to right — so plan and apply produce identical ids
    for an unchanged document. `include` filters which ids become pairs; ids
    are consumed for every occurrence either way (and stop at
    MAX_SEARCH_RESULTS in both plan and apply). Returns (entries, truncated)
    with entries = (region, char ranges, match ids) per matched line region;
    non-editable regions are included so the caller can report them skipped.
    """
    regions = build_regions(engine.doc[page_index], doc_id, engine.revision,
                            include_paragraphs=False)
    for region in regions:
        cap = engine.capability(page_index, region)
        region.editable = cap.editable
        region.unsupported_reason = "" if cap.editable else cap.reason
    grouped: dict[int, tuple[TextRegion, list[tuple[int, int]], list[int]]] = {}
    truncated = False
    for region in regions:
        line = region.lines[0]
        for start, end in find_line_matches(line.text, query, match_case, whole_word):
            mid = next(counter)
            if mid >= MAX_SEARCH_RESULTS:
                truncated = True
                return list(grouped.values()), True
            if include is not None and mid not in include:
                continue
            entry = grouped.setdefault(id(region), (region, [], []))
            entry[1].append((start, end))
            entry[2].append(mid)
    return list(grouped.values()), truncated


def _record_pages(record: EditRecord) -> list[int]:
    """Pages a committed record touched (grouped batches report all of them)."""
    if record.pairs:
        return sorted({edit.page_index for _, edit in record.pairs})
    return [record.page_index]


@dataclass
class _DocState:
    engine: PdfEditEngine
    path: str | None
    fingerprint: str
    was_encrypted: bool = False  # captured BEFORE authenticate() clears the flag
    undo: list[tuple[bytes, EditRecord]] = field(default_factory=list)
    redo: list[tuple[bytes, EditRecord]] = field(default_factory=list)
    prepared: dict[str, PreparedEdit] = field(default_factory=dict)
    changed_pages: dict[int, tuple[list[str], list[str]]] = field(default_factory=dict)
    # page -> ([expected texts], [forbidden texts]) accumulated across edits;
    # lists since a Replace-All batch rebuilds several lines of one page (M10)
    batch: dict[str, _BatchPlan] = field(default_factory=dict)


class PdfWorker:
    """Serial request handler. One instance per worker process."""

    def __init__(self) -> None:
        self.docs: dict[str, _DocState] = {}

    # -- dispatch ----------------------------------------------------------
    def handle(self, req: Request) -> Result | None:
        try:
            method = getattr(self, f"_on_{req.kind}", None)
            if method is None:
                return Result.failure(req, f"unknown request kind: {req.kind}")
            return method(req)
        except SaveError as exc:
            return Result.failure(req, str(exc))
        except FileNotFoundError as exc:
            return Result.failure(req, f"file not found: {exc}")
        except Exception as exc:  # worker must survive any single bad request
            tb = traceback.format_exc(limit=6)
            return Result.failure(req, f"{type(exc).__name__}: {exc}\n{tb}")

    def _doc(self, req: Request) -> _DocState:
        state = self.docs.get(req.doc_id or "")
        if state is None:
            raise KeyError(f"unknown document: {req.doc_id}")
        return state

    # -- open / close / info ------------------------------------------------
    def _on_open(self, req: Request) -> Result:
        """Open from a path, or from recovery bytes with `{"data": ...}`."""
        password = req.payload.get("password") or ""
        data = req.payload.get("data")
        if data is not None:
            return self._open_bytes(req, data, password)
        path = req.payload["path"]
        doc = pymupdf.open(path)
        was_encrypted = bool(doc.is_encrypted)
        if doc.needs_pass:
            if not password or not doc.authenticate(password):
                doc.close()
                return Result(request_id=req.request_id, kind=req.kind,
                              revision=None, ok=True,
                              payload={"needs_password": True, "path": path})
        doc_id = req.doc_id or uuid.uuid4().hex
        engine = PdfEditEngine(doc, doc_id)
        state = _DocState(engine=engine, path=path,
                          fingerprint=file_fingerprint(path),
                          was_encrypted=was_encrypted)
        self.docs[doc_id] = state
        meta = self._meta(state, doc_id)
        return Result(request_id=req.request_id, kind=req.kind, doc_id=doc_id,
                      revision=engine.revision, ok=True,
                      payload={"meta": meta, "needs_password": False,
                               "page_sizes": [
                                   (page_geometry(p).width, page_geometry(p).height)
                                   for p in doc
                               ],
                               "rotations": [page_geometry(p).rotation for p in doc]})

    def _open_bytes(self, req: Request, data: bytes, password: str) -> Result:
        """Open a recovered session: bytes are the edited document; `source_path`
        and `fingerprint` describe the (possibly changed) original file."""
        source = req.payload.get("source_path") or ""
        try:
            doc = pymupdf.open(stream=data)
        except Exception as exc:
            return Result.failure(req, f"Recovery data is damaged: {exc}")
        if doc.needs_pass and not doc.authenticate(password):
            doc.close()
            return Result.failure(req, "Recovery data needs a password.")
        doc_id = req.doc_id or uuid.uuid4().hex
        engine = PdfEditEngine(doc, doc_id)
        # keep the recovered revision counter; in-memory undo history is lost
        engine.revision = int(req.payload.get("revision", 0))
        state = _DocState(engine=engine, path=source,
                          fingerprint=req.payload.get("fingerprint", ""),
                          was_encrypted=bool(doc.is_encrypted))
        self.docs[doc_id] = state
        meta = self._meta(state, doc_id)
        meta.path = source
        return Result(request_id=req.request_id, kind=req.kind, doc_id=doc_id,
                      revision=engine.revision, ok=True,
                      payload={"meta": meta, "needs_password": False,
                               "restored": True,
                               "recovered_revision": req.payload.get("revision", 0),
                               "page_sizes": [
                                   (page_geometry(p).width, page_geometry(p).height)
                                   for p in doc
                               ],
                               "rotations": [page_geometry(p).rotation for p in doc]})

    def _on_snapshot(self, req: Request) -> Result:
        state = self._doc(req)
        data = state.engine.doc.tobytes(deflate=True)
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=True,
                      payload={"data": data, "revision": state.engine.revision,
                               "path": state.path})

    def _on_auth(self, req: Request) -> Result:
        path = req.payload["path"]
        password = req.payload.get("password") or ""
        doc = pymupdf.open(path)
        was_encrypted = bool(doc.is_encrypted)
        if not doc.authenticate(password):
            doc.close()
            return Result(request_id=req.request_id, kind=req.kind, ok=False,
                          error="wrong-password")
        doc_id = req.doc_id or uuid.uuid4().hex
        engine = PdfEditEngine(doc, doc_id)
        state = _DocState(engine=engine, path=path,
                          fingerprint=file_fingerprint(path),
                          was_encrypted=was_encrypted)
        self.docs[doc_id] = state
        return Result(request_id=req.request_id, kind=req.kind, doc_id=doc_id,
                      revision=0, ok=True,
                      payload={"meta": self._meta(state, doc_id),
                               "page_sizes": [
                                   (page_geometry(p).width, page_geometry(p).height)
                                   for p in doc
                               ],
                               "rotations": [page_geometry(p).rotation for p in doc]})

    def _meta(self, state: _DocState, doc_id: str) -> DocumentMeta:
        doc = state.engine.doc
        return DocumentMeta(
            doc_id=doc_id,
            path=state.path,
            page_count=doc.page_count,
            is_encrypted=state.was_encrypted,
            needs_pass=False,  # we only get here once the doc is actually open
            is_signed=has_signature(doc),
            permissions=int(doc.permissions),
            revision=state.engine.revision,
            fingerprint=state.fingerprint,
            title=str(doc.metadata.get("title") or ""),
        )

    def _on_doc_info(self, req: Request) -> Result:
        state = self._doc(req)
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision,
                      payload={"meta": self._meta(state, req.doc_id or "")})

    def _on_close(self, req: Request) -> Result:
        state = self.docs.pop(req.doc_id or "", None)
        if state:
            try:
                state.engine.doc.close()
            except Exception:
                pass
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      payload={})

    # -- rendering -----------------------------------------------------------
    def _on_render_page(self, req: Request) -> Result:
        state = self._doc(req)
        page_index = req.payload["page"]
        zoom = float(req.payload.get("zoom", 1.0))
        dpr = float(req.payload.get("dpr", 1.0))
        clip = req.payload.get("clip")
        if req.revision is not None and req.revision != state.engine.revision:
            return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                          revision=state.engine.revision, ok=False, error="stale-revision")
        page = state.engine.doc[page_index]
        matrix = pymupdf.Matrix(zoom * dpr, zoom * dpr)
        pix = page.get_pixmap(matrix=matrix, alpha=False,
                              clip=pymupdf.Rect(*clip) if clip else None)
        geom = page_geometry(page)
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=True,
                      payload={
                          "page": page_index, "zoom": zoom, "dpr": dpr,
                          "png": pix.tobytes("png"),
                          "width_px": pix.width, "height_px": pix.height,
                          "page_width": geom.width, "page_height": geom.height,
                          "rotation": geom.rotation,
                      })

    def _on_render_thumbnail(self, req: Request) -> Result:
        state = self._doc(req)
        page_index = req.payload["page"]
        target_w = int(req.payload.get("target_width", 120))
        page = state.engine.doc[page_index]
        geom = page_geometry(page)
        zoom = max(0.02, target_w / max(1.0, geom.width))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=True,
                      payload={"page": page_index, "png": pix.tobytes("png"),
                               "width_px": pix.width, "height_px": pix.height})

    # -- extraction ------------------------------------------------------------
    def _on_extract_regions(self, req: Request) -> Result:
        state = self._doc(req)
        page_index = req.payload["page"]
        include_paragraphs = bool(req.payload.get("paragraphs", True))
        regions = build_regions(state.engine.doc[page_index], req.doc_id or "",
                                state.engine.revision, include_paragraphs)
        for region in regions:
            cap = state.engine.capability(page_index, region)
            region.editable = cap.editable
            region.unsupported_reason = "" if cap.editable else cap.reason
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=True,
                      payload={"page": page_index, "regions": regions})

    def _on_extract_page_text(self, req: Request) -> Result:
        state = self._doc(req)
        page_index = req.payload["page"]
        lines = extract_page_lines(state.engine.doc[page_index], with_chars=False)
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=True,
                      payload={"page": page_index,
                               "lines": [(ln.text, ln.bbox.as_tuple(), ln.baseline_y)
                                         for ln in lines]})

    def _on_search(self, req: Request) -> Result:
        state = self._doc(req)
        query = req.payload.get("query", "")
        match_case = bool(req.payload.get("match_case", False))
        if not query:
            return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                          revision=state.engine.revision, payload={"results": []})
        results: list[dict] = []
        doc = state.engine.doc
        for page_index in range(doc.page_count):
            page = doc[page_index]
            for line in extract_page_lines(page):
                # find_line_matches normalizes in NFKC space (D20), so a
                # logical-Arabic needle finds presentation-form extracted text
                for start, end in find_line_matches(line.text, query,
                                                    match_case=match_case):
                    rect = self._match_rect(line, start, end - start)
                    results.append({
                        "page": page_index,
                        "rect": rect.as_tuple() if rect else None,
                        "snippet": line.text.strip()[:160],
                    })
                    if len(results) >= MAX_SEARCH_RESULTS:
                        break
            if len(results) >= MAX_SEARCH_RESULTS:
                break
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=True,
                      payload={"results": results, "query": query,
                               "truncated": len(results) >= MAX_SEARCH_RESULTS})

    @staticmethod
    def _match_rect(line, pos: int, length: int) -> Rect | None:
        """Char-accurate rect for line.text[pos:pos+length]."""
        boxes: list[Rect] = []
        i = 0
        for run in line.runs:
            run_len = len(run.text)
            lo, hi = max(pos, i), min(pos + length, i + run_len)
            if lo < hi and run.char_bboxes and len(run.char_bboxes) == run_len:
                boxes.extend(run.char_bboxes[lo - i:hi - i])
            elif lo < hi:
                boxes.append(run.bbox)
            i += run_len
        if not boxes:
            return None
        return Rect(min(b.x0 for b in boxes), min(b.y0 for b in boxes),
                    max(b.x1 for b in boxes), max(b.y1 for b in boxes))

    # -- editing -----------------------------------------------------------------
    def _on_prepare_edit(self, req: Request) -> Result:
        state = self._doc(req)
        preview_zoom = float(req.payload.get("preview_zoom", 2.0))
        if req.payload.get("insert"):
            # Add Text: pure insertion into an empty box; no source region
            edit: ReplacementEdit = req.payload["edit"]
            box = Rect(*req.payload["box"])
            prepared = state.engine.prepare_insert(
                int(req.payload.get("page", edit.page_index)), box, edit,
                preview_zoom=preview_zoom)
        else:
            region: TextRegion = req.payload["region"]
            edit = req.payload["edit"]
            prepared = state.engine.prepare(region_page(region), region, edit,
                                            preview_zoom=preview_zoom)
        key = uuid.uuid4().hex
        if prepared.ok:
            # keep candidate server-side; only validation + preview cross the pipe
            state.prepared[key] = prepared
            if len(state.prepared) > 8:  # evict oldest beyond a small cap
                for old in list(state.prepared)[:len(state.prepared) - 8]:
                    state.prepared.pop(old, None)
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=prepared.ok,
                      error=None if prepared.ok else (prepared.issues[0] if prepared.issues else "edit failed"),
                      payload={"validation": prepared.validation,
                               "preview_png": prepared.preview_png,
                               "before_png": prepared.before_png,
                               "prepare_key": key if prepared.ok else None,
                               "issues": prepared.issues})

    def _on_prepare_redact(self, req: Request) -> Result:
        """Black-out marks (M9): true removal of text under engine-space boxes."""
        state = self._doc(req)
        preview_zoom = float(req.payload.get("preview_zoom", 2.0))
        page_index = int(req.payload["page"])
        boxes = [Rect(*b) for b in req.payload["boxes"]]
        prepared = state.engine.prepare_redact(page_index, boxes,
                                               preview_zoom=preview_zoom)
        key = uuid.uuid4().hex
        if prepared.ok:
            state.prepared[key] = prepared
            if len(state.prepared) > 8:
                for old in list(state.prepared)[:len(state.prepared) - 8]:
                    state.prepared.pop(old, None)
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=prepared.ok,
                      error=None if prepared.ok else (prepared.issues[0] if prepared.issues else "redaction failed"),
                      payload={"validation": prepared.validation,
                               "preview_png": prepared.preview_png,
                               "before_png": prepared.before_png,
                               "prepare_key": key if prepared.ok else None,
                               "issues": prepared.issues})

    def _account_commit(self, state: _DocState, record: EditRecord) -> None:
        """Fold a committed record into `changed_pages` (save-time checks, §12).

        List-valued since M10: one page can carry several rebuilt lines from a
        Replace-All batch, and every one of them is verified at save.
        """
        entries = record.pairs or [(record.region, record.edit)]
        for region, edit in entries:
            want = norm_cmp(edit.new_text)
            gone = (norm_cmp(record.redacted_text) if region is None
                    else norm_cmp(region.text))
            exp, forb = state.changed_pages.get(record.page_index, ([], []))
            if want and want not in exp:
                exp.append(want)
            if gone and gone != want and gone not in forb:
                forb.append(gone)
            state.changed_pages[record.page_index] = (exp, forb)

    def _on_commit_edit(self, req: Request) -> Result:
        state = self._doc(req)
        key = req.payload["prepare_key"]
        prepared = state.prepared.pop(key, None)
        if prepared is None or not prepared.ok:
            return Result.failure(req, "prepared edit expired; re-run the edit")
        pre_bytes = prepared.pre_bytes
        record = prepared.record
        new_rev = state.engine.commit(prepared)
        self._account_commit(state, record)
        state.undo.append((pre_bytes, record))
        if len(state.undo) > UNDO_CAP:
            state.undo.pop(0)
        state.redo.clear()
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=new_rev, ok=True,
                      payload={"revision": new_rev, "page": record.page_index,
                               "can_undo": bool(state.undo), "can_redo": False})

    # -- replace all (M10) --------------------------------------------------------
    def _on_replace_plan(self, req: Request) -> Result:
        """Find every Replace-All match and build per-page candidates (M10).

        Nothing is committed here: candidates exist so the review window can
        show honest before/after renders and per-match skip reasons. The batch
        is remembered worker-side; apply re-runs the same deterministic scan.
        """
        state = self._doc(req)
        p = req.payload
        query = str(p.get("query") or "")
        replacement = str(p.get("replacement") or "")
        match_case = bool(p.get("match_case"))
        whole_word = bool(p.get("whole_word"))
        auto_shrink = bool(p.get("auto_shrink"))
        preview_zoom = float(p.get("preview_zoom", 2.0))
        if not query:
            return Result.failure(req, "Enter the text to find.")
        engine = state.engine
        counter = itertools.count(0)
        matches: dict[int, dict] = {}
        prepared_pages: dict[int, PreparedEdit] = {}
        page_rects: dict[int, tuple] = {}
        truncated = False
        for page_index in range(engine.doc.page_count):
            entries, trunc = _match_page_regions(
                engine, req.doc_id or "", page_index, query,
                match_case, whole_word, counter)
            truncated = truncated or trunc
            pairs: list[BatchPair] = []
            for region, ranges, ids in entries:
                snippet = region.lines[0].text.strip()[:160]
                if not region.editable:
                    for mid in ids:
                        matches[mid] = {
                            "id": mid, "page": page_index, "snippet": snippet,
                            "status": "skipped",
                            "reason": region.unsupported_reason or "This line cannot be edited.",
                            "used_size": None, "font": "", "font_substituted": False,
                        }
                    continue
                edit = ReplacementEdit(
                    region_id=region.region_id, source_revision=engine.revision,
                    new_text=apply_ranges(region.lines[0].text, ranges, replacement),
                    mode=EditMode.PRESERVE_LINE, auto_shrink=auto_shrink,
                    page_index=page_index)
                pairs.append(BatchPair(region=region, edit=edit, match_ids=ids))
                for mid in ids:
                    matches[mid] = {
                        "id": mid, "page": page_index, "snippet": snippet,
                        "status": "planned", "reason": "",
                        "used_size": None, "font": "", "font_substituted": False,
                    }
            if not pairs:
                if truncated:
                    break
                continue
            prepared, outcomes, change_rect = engine.prepare_page_edits(
                page_index, pairs, preview_zoom)
            outcome_by_id: dict[int, PairOutcome] = {}
            for o in outcomes:
                for mid in o.match_ids:
                    outcome_by_id[mid] = o
            ok = prepared is not None and prepared.ok
            fallback = (prepared.issues[0]
                        if prepared is not None and prepared.issues else None)
            for mid, m in matches.items():
                if m["page"] != page_index or m["status"] != "planned":
                    continue
                o = outcome_by_id.get(mid)
                if o is not None and o.status == "skipped":
                    m.update(status="skipped", reason=o.reason)
                elif not ok:
                    m.update(status="skipped",
                             reason=fallback or "The edit was rejected during validation.")
                elif o is not None:
                    m.update(used_size=o.used_size, font=o.font_family,
                             font_substituted=o.font_substituted, reason=o.reason)
            if ok:
                prepared_pages[page_index] = prepared
                page_rects[page_index] = (
                    (change_rect.x0, change_rect.y0, change_rect.x1, change_rect.y1)
                    if change_rect is not None else None)
            if truncated:
                break

        batch = _BatchPlan(
            key=uuid.uuid4().hex, revision=engine.revision, query=query,
            replacement=replacement, match_case=match_case, whole_word=whole_word,
            auto_shrink=auto_shrink, matches=matches, prepared=prepared_pages,
            page_rects=page_rects, preview_zoom=preview_zoom)
        state.batch[batch.key] = batch
        if len(state.batch) > MAX_BATCH_PLANS:
            for old in list(state.batch)[:len(state.batch) - MAX_BATCH_PLANS]:
                state.batch.pop(old, None)

        ready = sum(1 for m in matches.values() if m["status"] == "planned")
        pages_payload = []
        for i, (page_index, prepared) in enumerate(prepared_pages.items()):
            pages_payload.append({
                "page": page_index,
                "change_rect": batch.page_rects.get(page_index),
                "preview_zoom": preview_zoom,
                "before_png": prepared.before_png if i < MAX_PREVIEW_PAGES else None,
                "after_png": prepared.preview_png if i < MAX_PREVIEW_PAGES else None,
            })
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=engine.revision, ok=True,
                      payload={"batch_key": batch.key, "revision": engine.revision,
                               "query": query,
                               "matches": [matches[i] for i in sorted(matches)],
                               "pages": pages_payload,
                               "ready": ready,
                               "skipped": len(matches) - ready,
                               "total": len(matches),
                               "truncated": truncated})

    def _on_replace_apply(self, req: Request) -> Result:
        """Commit the reviewed Replace-All pages as ONE undoable group (M10).

        Candidates cannot be reused across commits (each is a whole-document
        snapshot), so apply re-runs plan's deterministic scan page by page at
        the current revision — prepare → commit → next page — and pushes a
        single undo entry built from the first page's pre-batch bytes.
        """
        state = self._doc(req)
        p = req.payload
        batch = state.batch.pop(str(p.get("batch_key") or ""), None)
        if batch is None:
            return Result.failure(req, "This replace plan expired — run Replace All again.")
        include = {int(i) for i in (p.get("include_ids") or [])}
        if not include:
            return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                          revision=state.engine.revision, ok=True,
                          payload={"revision": state.engine.revision, "pages": [],
                                   "replaced": 0, "skipped": [],
                                   "query": batch.query,
                                   "can_undo": bool(state.undo),
                                   "can_redo": False})
        engine = state.engine
        if engine.revision != batch.revision:
            return Result.failure(req, "The document changed since the review — "
                                       "run Replace All again.")
        counter = itertools.count(0)
        pages_changed: list[int] = []
        merged_pairs: list[tuple[TextRegion, ReplacementEdit]] = []
        first_pre: bytes | None = None
        replaced = 0
        skipped: list[dict] = []
        for page_index in range(engine.doc.page_count):
            entries, _trunc = _match_page_regions(
                engine, req.doc_id or "", page_index, batch.query,
                batch.match_case, batch.whole_word, counter, include=include)
            pairs = []
            for region, ranges, ids in entries:
                if not region.editable:
                    continue
                pairs.append(BatchPair(
                    region=region,
                    edit=ReplacementEdit(
                        region_id=region.region_id, source_revision=engine.revision,
                        new_text=apply_ranges(region.lines[0].text, ranges,
                                              batch.replacement),
                        mode=EditMode.PRESERVE_LINE, auto_shrink=batch.auto_shrink,
                        page_index=page_index),
                    match_ids=ids))
            if not pairs:
                continue
            prepared, outcomes, _rect = engine.prepare_page_edits(
                page_index, pairs, batch.preview_zoom)
            for o in outcomes:
                if o.status == "skipped":
                    skipped.append({"page": page_index, "match_ids": o.match_ids,
                                    "reason": o.reason})
            if prepared is None or not prepared.ok:
                issue = (prepared.issues[0] if prepared is not None and prepared.issues
                         else "The edit was rejected during validation.")
                for o in outcomes:
                    if o.status == "planned":
                        skipped.append({"page": page_index, "match_ids": o.match_ids,
                                        "reason": issue})
                continue
            if first_pre is None:
                first_pre = prepared.pre_bytes
            engine.commit(prepared)
            pages_changed.append(page_index)
            record = prepared.record
            merged_pairs.extend(record.pairs or [(record.region, record.edit)])
            replaced += sum(len(o.match_ids) for o in outcomes if o.status == "planned")
            self._account_commit(state, record)

        if pages_changed and first_pre is not None:
            merged_record = EditRecord(
                region=merged_pairs[0][0], edit=merged_pairs[0][1],
                resolved_family="", resolved_source="",
                page_index=pages_changed[0],
                pre_revision=batch.revision, post_revision=engine.revision,
                pairs=merged_pairs)
            # grouped undo: one entry for the whole batch (per-page pushes are
            # deliberately skipped — Ctrl+Z undoes the entire Replace All)
            state.undo.append((first_pre, merged_record))
            if len(state.undo) > UNDO_CAP:
                state.undo.pop(0)
            state.redo.clear()
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=engine.revision, ok=True,
                      payload={"revision": engine.revision, "pages": pages_changed,
                               "replaced": replaced, "skipped": skipped,
                               "query": batch.query,
                               "can_undo": bool(state.undo), "can_redo": False})

    def _swap_doc(self, state: _DocState, doc_bytes: bytes, revision: int) -> None:
        old = state.engine.doc
        state.engine.doc = pymupdf.open(stream=doc_bytes)
        state.engine.revision = revision
        try:
            old.close()
        except Exception:
            pass
        state.prepared.clear()

    def _on_undo(self, req: Request) -> Result:
        state = self._doc(req)
        if not state.undo:
            return Result.failure(req, "nothing to undo")
        pre_bytes, record = state.undo.pop()
        current = state.engine.doc.tobytes()
        state.redo.append((current, record))
        self._swap_doc(state, pre_bytes, record.pre_revision)
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=True,
                      payload={"revision": state.engine.revision,
                               "page": record.page_index,
                               "pages": _record_pages(record),
                               "can_undo": bool(state.undo),
                               "can_redo": bool(state.redo)})

    def _on_redo(self, req: Request) -> Result:
        state = self._doc(req)
        if not state.redo:
            return Result.failure(req, "nothing to redo")
        post_bytes, record = state.redo.pop()
        current = state.engine.doc.tobytes()
        state.undo.append((current, record))
        self._swap_doc(state, post_bytes, record.post_revision)
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=True,
                      payload={"revision": state.engine.revision,
                               "page": record.page_index,
                               "pages": _record_pages(record),
                               "can_undo": bool(state.undo),
                               "can_redo": bool(state.redo)})

    # -- saving ------------------------------------------------------------------
    def _on_save(self, req: Request) -> Result:
        state = self._doc(req)
        dest = req.payload["path"]
        encryption = req.payload.get("encryption", "keep")
        # never overwrite a source file that changed on disk since it was opened
        try:
            same = bool(state.path) and os.path.abspath(dest) == os.path.abspath(state.path)
        except (TypeError, ValueError):
            same = False
        if same and state.fingerprint:
            try:
                current = file_fingerprint(dest)
            except OSError:
                current = None
            if current is not None and current != state.fingerprint:
                return Result.failure(
                    req, "The file changed on disk since it was opened (another "
                    "program may have saved it). Use Save As to a new file so "
                    "those changes are not lost.")
        checks = SaveChecks(
            page_count=state.engine.doc.page_count,
            changed_pages=set(state.changed_pages),
            expected_text={p: v[0] for p, v in state.changed_pages.items() if v[0]},
            forbidden_text={p: v[1] for p, v in state.changed_pages.items() if v[1]},
        )
        saved = save_validated(state.engine.doc, dest, checks, encryption=encryption,
                               was_encrypted=state.was_encrypted)
        state.path = str(saved)
        state.fingerprint = file_fingerprint(saved)
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=True,
                      payload={"path": str(saved), "fingerprint": state.fingerprint,
                               "signed": has_signature(state.engine.doc)})

    # -- export (PDF -> DOCX) --------------------------------------------------
    def _on_export_docx(self, req: Request) -> Result:
        """Render the open document to a .docx file. Does not mutate the PDF."""
        from pathlib import Path as _Path
        state = self._doc(req)
        dest = _Path(req.payload["path"])
        opts_payload = req.payload.get("options") or {}
        options = DocxExportOptions(
            embed_images=bool(opts_payload.get("embed_images", True)),
            detect_tables=bool(opts_payload.get("detect_tables", True)),
            detect_columns=bool(opts_payload.get("detect_columns", True)),
            flow_mode=str(opts_payload.get("flow_mode", "formatted")),
        )
        try:
            result = write_docx_atomic(state.engine.doc, options, dest)
        except DocxExportError as exc:
            return Result.failure(req, str(exc))
        except FileNotFoundError as exc:
            return Result.failure(req, f"file not found: {exc}")
        # serialise the result so it crosses the pipe cleanly
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=state.engine.revision, ok=True,
                      payload={
                          "result": {
                              "ok": result.ok,
                              "output_path": result.output_path,
                              "pages_written": result.pages_written,
                              "unsupported_items": [
                                  {"page_index": u.page_index, "kind": u.kind,
                                   "message": u.message}
                                  for u in result.unsupported_items
                              ],
                              "warnings": list(result.warnings),
                              "error": result.error,
                          }
                      })


def region_page(region: TextRegion) -> int:
    part = region.region_id.split(":p", 1)[1]
    return int(part.split(":", 1)[0])


# -- process plumbing ---------------------------------------------------------

def worker_main(conn) -> None:
    """Child-process entry: serial recv/handle/send loop."""
    worker = PdfWorker()
    while True:
        try:
            if not conn.poll(0.25):
                continue
            req = conn.recv()
        except (EOFError, OSError):
            break  # parent went away
        except KeyboardInterrupt:  # pragma: no cover
            break
        if req.kind == SHUTDOWN:
            try:
                conn.send(Result(request_id=req.request_id, kind=SHUTDOWN))
            except (BrokenPipeError, OSError):
                pass
            break
        result = worker.handle(req)
        if result is None:
            continue
        try:
            conn.send(result)
        except (BrokenPipeError, OSError):
            break
    for state in worker.docs.values():
        try:
            state.engine.doc.close()
        except Exception:
            pass


def spawn_worker():
    """Start a worker process. Returns (process, parent_conn).

    The pipe is duplex: the parent sends requests and receives results on the
    same connection (from different threads — sends are lock-guarded client-side).
    """
    parent_conn, child_conn = multiprocessing.Pipe(duplex=True)
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=worker_main, args=(child_conn,),
                       name="openpdfsuite-pdf-worker", daemon=True)
    proc.start()
    child_conn.close()  # parent side does not need the child end
    return proc, parent_conn
