"""Application-layer controller: user actions -> worker requests -> UI signals.

One controller per document window. It owns the WorkerClient (and therefore the
worker process), the DocumentSession, and the render cache. It performs
staleness rejection: results whose doc_id/revision no longer match, or whose
request has been superseded, are dropped before reaching widgets (AGENTS.md §7).

No document mutation logic lives here — this only coordinates (AGENTS.md §6).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap

from ..domain.models import DocxExportOptions, DocxExportResult, ReplacementEdit, TextRegion
from ..infrastructure.logging import get_logger
from ..infrastructure.pdf import protocol as P
from .render_cache import CacheKey, RenderCache
from .session import DocumentSession
from .worker_client import WorkerClient

log = get_logger("controller")


class DocumentController(QObject):
    # lifecycle
    document_opened = Signal(object, list)  # DocumentMeta, page_sizes
    needs_password = Signal(str)  # path
    document_closed = Signal()
    failure = Signal(str)  # user-facing message
    worker_crashed = Signal(str)

    # rendering
    page_rendered = Signal(int, object, float, int)  # page, QPixmap, zoom, request_id
    thumbnail_rendered = Signal(int, object)  # page, QPixmap

    # content
    page_lines_ready = Signal(int, list, int)  # page, [(text, rect, baseline)], revision
    regions_ready = Signal(int, list, int)  # page, [TextRegion], revision
    search_done = Signal(list, bool)  # results, truncated

    # editing
    edit_prepared = Signal(dict)  # {ok, validation, preview_png, prepare_key, issues}
    snapshot_ready = Signal(str, int, bytes, str)  # doc_id, revision, data, path
    save_failed = Signal(str)
    edit_committed = Signal(int, int)  # revision, page
    undo_redo_done = Signal(int, bool, bool, int)  # revision, can_undo, can_redo, page
    saved = Signal(str, str)  # path, fingerprint
    docx_exported = Signal(object)  # DocxExportResult

    def __init__(self, cache_budget: int | None = None, parent=None):
        super().__init__(parent)
        self.client = WorkerClient(self)
        self.cache = RenderCache(cache_budget) if cache_budget else RenderCache()
        self.session: DocumentSession | None = None
        self._render_supersede: dict[int, int] = {}  # page -> newest request_id
        self._open_path: str | None = None
        self.client.result_ready.connect(self._on_result)
        self.client.worker_died.connect(self.worker_crashed)

    # -- commands ---------------------------------------------------------
    def open(self, path: str | Path, password: str = "") -> None:
        path = str(path)
        self._open_path = path
        rid = self.client.send(P.OPEN, {"path": path, "password": password})
        self._open_request_id = rid

    def snapshot(self) -> None:
        """Request the worker's current document bytes (recovery, §11)."""
        if self.session:
            self.client.send(P.SNAPSHOT, doc_id=self.session.doc_id)

    def open_bytes(self, data: bytes, source_path: str, fingerprint: str,
                   revision: int) -> None:
        """Open a recovered session (edited bytes + original source info)."""
        if self.session is not None:
            self.close_document()
        rid = self.client.send(P.OPEN, {
            "data": data, "source_path": source_path,
            "fingerprint": fingerprint, "revision": revision})
        self._open_request_id = rid

    def close_document(self) -> None:
        if self.session:
            self.client.send(P.CLOSE, doc_id=self.session.doc_id)
            self.cache.invalidate_doc(self.session.doc_id)
            self.session = None
            self.document_closed.emit()

    def shutdown(self) -> None:
        self.client.shutdown()

    def request_page(self, page: int, zoom: float, dpr: float) -> int:
        """Request a full-page render; supersedes older requests for the page."""
        assert self.session
        cached = self.cache.get(CacheKey(self.session.doc_id, self.session.revision,
                                         page, zoom, dpr))
        if cached is not None:
            self.page_rendered.emit(page, cached, zoom, -1)
            return -1
        rid = self.client.send(P.RENDER_PAGE, {"page": page, "zoom": zoom, "dpr": dpr},
                               doc_id=self.session.doc_id,
                               revision=self.session.revision)
        self._render_supersede[page] = rid
        return rid

    def request_thumbnails(self, pages: list[int], target_width: int = 120) -> None:
        assert self.session
        for page in pages:
            self.client.send(P.RENDER_THUMBNAIL,
                             {"page": page, "target_width": target_width},
                             doc_id=self.session.doc_id)

    def request_page_lines(self, page: int) -> None:
        assert self.session
        self.client.send(P.EXTRACT_PAGE_TEXT, {"page": page},
                         doc_id=self.session.doc_id, revision=self.session.revision)

    def request_regions(self, page: int, paragraphs: bool = True) -> None:
        assert self.session
        self.client.send(P.EXTRACT_REGIONS, {"page": page, "paragraphs": paragraphs},
                         doc_id=self.session.doc_id, revision=self.session.revision)

    def search(self, query: str, match_case: bool = False) -> None:
        assert self.session
        self.client.send(P.SEARCH, {"query": query, "match_case": match_case},
                         doc_id=self.session.doc_id, revision=self.session.revision)

    def prepare_edit(self, page: int, region: TextRegion,
                     edit: ReplacementEdit, preview_zoom: float = 2.0) -> None:
        assert self.session
        self.client.send(P.PREPARE_EDIT,
                         {"region": region, "edit": edit, "preview_zoom": preview_zoom},
                         doc_id=self.session.doc_id, revision=self.session.revision)

    def prepare_insert(self, page: int, box: tuple, edit: ReplacementEdit,
                       preview_zoom: float = 2.0) -> None:
        """Add Text: pure insertion into an empty box (no source region)."""
        assert self.session
        self.client.send(P.PREPARE_EDIT,
                         {"insert": True, "page": page, "box": list(box),
                          "edit": edit, "preview_zoom": preview_zoom},
                         doc_id=self.session.doc_id, revision=self.session.revision)

    def commit_edit(self, prepare_key: str) -> None:
        assert self.session
        self.client.send(P.COMMIT_EDIT, {"prepare_key": prepare_key},
                         doc_id=self.session.doc_id, revision=self.session.revision)

    def undo(self) -> None:
        assert self.session
        self.client.send(P.UNDO, doc_id=self.session.doc_id)

    def redo(self) -> None:
        assert self.session
        self.client.send(P.REDO, doc_id=self.session.doc_id)

    def save(self, dest: str, encryption: str = "keep") -> None:
        assert self.session
        self.client.send(P.SAVE, {"path": dest, "encryption": encryption},
                         doc_id=self.session.doc_id, revision=self.session.revision)

    def export_docx(self, dest: str | Path,
                    options: DocxExportOptions | None = None) -> None:
        """Convert the open PDF to a .docx file. Does not mutate the PDF."""
        assert self.session
        opts = options or DocxExportOptions()
        self.client.send(P.EXPORT_DOCX,
                         {"path": str(dest),
                          "options": {
                              "embed_images": opts.embed_images,
                              "detect_tables": opts.detect_tables,
                              "detect_columns": opts.detect_columns,
                              "flow_mode": opts.flow_mode,
                          }},
                         doc_id=self.session.doc_id,
                         revision=self.session.revision)

    def check_source_unchanged(self) -> bool:
        """Compare the on-disk fingerprint of the source file (recovery, §11)."""
        if not self.session or not self.session.path:
            return True
        from ..infrastructure.pdf.worker import file_fingerprint

        try:
            current = file_fingerprint(self.session.path)
        except OSError:
            return False
        return current == self.session.fingerprint

    # -- result routing ------------------------------------------------------
    def _on_result(self, res: P.Result) -> None:
        if res.kind == P.OPEN:
            self._on_opened(res)
        elif res.kind == P.AUTH:
            self._on_opened(res)
        elif res.kind == P.RENDER_PAGE:
            self._on_page_rendered(res)
        elif res.kind == P.RENDER_THUMBNAIL:
            self._on_thumbnail(res)
        elif res.kind == P.EXTRACT_PAGE_TEXT:
            self._on_page_lines(res)
        elif res.kind == P.EXTRACT_REGIONS:
            self._on_regions(res)
        elif res.kind == P.SEARCH:
            self._on_search(res)
        elif res.kind == P.PREPARE_EDIT:
            self._on_prepared(res)
        elif res.kind == P.COMMIT_EDIT:
            self._on_committed(res)
        elif res.kind in (P.UNDO, P.REDO):
            self._on_undo_redo(res)
        elif res.kind == P.SAVE:
            self._on_saved(res)
        elif res.kind == P.EXPORT_DOCX:
            self._on_docx_exported(res)
        elif res.kind == P.SNAPSHOT:
            if not self._stale(res):
                self.snapshot_ready.emit(res.doc_id, res.payload.get("revision", 0),
                                         res.payload.get("data", b""),
                                         res.payload.get("path", ""))
        elif res.kind == P.CLOSE:
            pass
        elif not res.ok:
            self._report(res)

    def _report(self, res: P.Result) -> None:
        message = res.error or "unknown error"
        log.error("worker error kind=%s: %s", res.kind, message.splitlines()[0])
        self.failure.emit(message)

    def _on_opened(self, res: P.Result) -> None:
        if not res.ok:
            self._report(res)
            return
        if res.payload.get("needs_password"):
            self.needs_password.emit(res.payload.get("path", self._open_path or ""))
            return
        meta = res.payload["meta"]
        page_sizes = [tuple(s) for s in res.payload.get("page_sizes", [])]
        self.session = DocumentSession(path=Path(meta.path) if meta.path else None)
        self.session.doc_id = meta.doc_id
        self.session.meta = meta
        self.session.page_sizes = page_sizes
        self.session.revision = meta.revision
        self.session.fingerprint = meta.fingerprint
        if res.payload.get("restored"):
            # recovered session: unsaved edits present, no undo history
            self.session.saved_revision = 0
            self.session.dirty = meta.revision > 0
            self.session.can_undo = False
            self.session.can_redo = False
        log.info("opened doc_id=%s pages=%d encrypted=%s signed=%s",
                 meta.doc_id, meta.page_count, meta.is_encrypted, meta.is_signed)
        self.document_opened.emit(meta, page_sizes)

    def _stale(self, res: P.Result) -> bool:
        if self.session is None or res.doc_id != self.session.doc_id:
            return True
        if res.revision is not None and res.revision != self.session.revision:
            return True
        return False

    def _on_page_rendered(self, res: P.Result) -> None:
        if not res.ok:
            if res.error == "stale-revision":
                return  # a newer revision exists; the view will re-request
            self._report(res)
            return
        if self._stale(res):
            return
        page = res.payload["page"]
        newest = self._render_supersede.get(page)
        if newest is not None and newest != res.request_id and newest != -1:
            return  # superseded by a newer zoom/scroll request
        png = res.payload["png"]
        pixmap = QPixmap()
        pixmap.loadFromData(png)
        dpr = res.payload.get("dpr", 1.0)
        pixmap.setDevicePixelRatio(dpr)
        zoom = res.payload["zoom"]
        key = CacheKey(self.session.doc_id, res.revision, page, zoom, dpr)
        self.cache.put(key, pixmap)
        self.page_rendered.emit(page, pixmap, zoom, res.request_id)

    def _on_thumbnail(self, res: P.Result) -> None:
        if not res.ok or self._stale(res):
            return
        pixmap = QPixmap()
        pixmap.loadFromData(res.payload["png"])
        self.thumbnail_rendered.emit(res.payload["page"], pixmap)

    def _on_page_lines(self, res: P.Result) -> None:
        if not res.ok or self._stale(res):
            return
        self.page_lines_ready.emit(res.payload["page"], res.payload["lines"],
                                   res.revision)

    def _on_regions(self, res: P.Result) -> None:
        if not res.ok or self._stale(res):
            return
        self.regions_ready.emit(res.payload["page"], res.payload["regions"],
                                res.revision)

    def _on_search(self, res: P.Result) -> None:
        if not res.ok:
            self._report(res)
            return
        if self._stale(res):
            return
        self.search_done.emit(res.payload.get("results", []),
                              bool(res.payload.get("truncated", False)))

    def _on_prepared(self, res: P.Result) -> None:
        # preparation failures are normal user feedback (overflow etc.), not crashes
        validation = res.payload.get("validation")
        issues = res.payload.get("issues") or (
            list(validation.issues) if validation is not None else []
        )
        if not issues and not res.ok:
            issues = [res.error or "The PDF engine rejected the edit without details."]
        self.edit_prepared.emit({
            "ok": res.ok,
            "validation": validation,
            "preview_png": res.payload.get("preview_png"),
            "before_png": res.payload.get("before_png"),
            "prepare_key": res.payload.get("prepare_key"),
            "issues": issues,
        })

    def _on_committed(self, res: P.Result) -> None:
        if not res.ok or self.session is None:
            self._report(res)
            return
        self.session.mark_committed(res.payload["revision"], res.payload["page"])
        self.cache.invalidate_revision(self.session.doc_id, self.session.revision)
        self.edit_committed.emit(self.session.revision, res.payload["page"])

    def _on_undo_redo(self, res: P.Result) -> None:
        if not res.ok or self.session is None:
            if not res.ok and res.error and "nothing to" in (res.error or ""):
                return  # benign
            self._report(res)
            return
        payload = res.payload
        self.session.mark_undo_redo(payload["revision"], payload["can_undo"],
                                    payload["can_redo"], payload["page"])
        self.cache.invalidate_revision(self.session.doc_id, self.session.revision)
        self.undo_redo_done.emit(payload["revision"], payload["can_undo"],
                                 payload["can_redo"], payload["page"])

    def _on_saved(self, res: P.Result) -> None:
        if not res.ok:
            self.save_failed.emit(res.error or "save failed")
            return
        if self.session is None:
            self._report(res)
            return
        path = res.payload["path"]
        self.session.path = Path(path)
        self.session.mark_saved(res.payload["fingerprint"])
        if res.payload.get("signed"):
            self.failure.emit(
                "Saved. Note: this document contains a digital signature whose "
                "validity changed because the content was edited."
            )
        self.saved.emit(path, self.session.fingerprint)

    def _on_docx_exported(self, res: P.Result) -> None:
        if self._stale(res):
            return
        if not res.ok:
            self.failure.emit(res.error or "PDF to Word conversion failed")
            return
        data = res.payload.get("result") or {}
        result = DocxExportResult(
            ok=bool(data.get("ok", True)),
            output_path=data.get("output_path"),
            pages_written=int(data.get("pages_written", 0)),
            warnings=list(data.get("warnings", [])),
            error=data.get("error"),
        )
        for raw in data.get("unsupported_items", []):
            from ..domain.models import UnsupportedItem
            result.unsupported_items.append(UnsupportedItem(
                page_index=int(raw.get("page_index", -1)),
                kind=str(raw.get("kind", "")),
                message=str(raw.get("message", "")),
            ))
        self.docx_exported.emit(result)
