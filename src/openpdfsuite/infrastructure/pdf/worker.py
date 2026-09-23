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

import multiprocessing
import os
import traceback
import uuid
from dataclasses import dataclass, field

import pymupdf

from ...domain.models import DocumentMeta, DocxExportOptions, Rect, ReplacementEdit, TextRegion
from ..docx.docx_writer import DocxExportError, write_docx_atomic
from .editor import EditRecord, PdfEditEngine, PreparedEdit
from .extractor import build_regions, extract_page_lines, page_geometry
from .protocol import (
    SHUTDOWN,
    Request,
    Result,
)
from .saver import SaveChecks, SaveError, fingerprint_file, has_signature, save_validated

UNDO_CAP = 32
MAX_SEARCH_RESULTS = 500


def file_fingerprint(path: str) -> str:
    return fingerprint_file(path)


@dataclass
class _DocState:
    engine: PdfEditEngine
    path: str | None
    fingerprint: str
    was_encrypted: bool = False  # captured BEFORE authenticate() clears the flag
    undo: list[tuple[bytes, EditRecord]] = field(default_factory=list)
    redo: list[tuple[bytes, EditRecord]] = field(default_factory=list)
    prepared: dict[str, PreparedEdit] = field(default_factory=dict)
    changed_pages: dict[int, tuple[str, str]] = field(default_factory=dict)
    # page -> (expected_text, forbidden_text) accumulated across edits


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
                               ]})

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
                               ]})

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
                               ]})

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
        needle = query if match_case else query.lower()
        results: list[dict] = []
        doc = state.engine.doc
        for page_index in range(doc.page_count):
            page = doc[page_index]
            for line in extract_page_lines(page):
                hay = line.text if match_case else line.text.lower()
                start = 0
                while (pos := hay.find(needle, start)) != -1 and len(results) < MAX_SEARCH_RESULTS:
                    rect = self._match_rect(line, pos, len(needle))
                    results.append({
                        "page": page_index,
                        "rect": rect.as_tuple() if rect else None,
                        "snippet": line.text.strip()[:160],
                    })
                    start = pos + max(1, len(needle))
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

    def _on_commit_edit(self, req: Request) -> Result:
        state = self._doc(req)
        key = req.payload["prepare_key"]
        prepared = state.prepared.pop(key, None)
        if prepared is None or not prepared.ok:
            return Result.failure(req, "prepared edit expired; re-run the edit")
        pre_bytes = prepared.pre_bytes
        record = prepared.record
        new_rev = state.engine.commit(prepared)
        page_index = record.page_index
        state.undo.append((pre_bytes, record))
        if len(state.undo) > UNDO_CAP:
            state.undo.pop(0)
        state.redo.clear()
        want = " ".join(record.edit.new_text.split())
        gone = " ".join(record.region.text.split()) if record.region is not None else ""
        exp, forb = state.changed_pages.get(page_index, ("", ""))
        state.changed_pages[page_index] = (
            want if want else exp,
            forb or (gone if gone != want else ""),
        )
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=new_rev, ok=True,
                      payload={"revision": new_rev, "page": page_index,
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
