"""Structured worker messages (AGENTS.md §7).

Every request and result carries a document/session identifier and a revision.
Results for stale revisions are discarded by the UI-side controller. Payloads
must be picklable: plain dicts of scalars, bytes, and domain dataclasses —
never live PyMuPDF objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Request kinds
OPEN = "open"
AUTH = "auth"
CLOSE = "close"
DOC_INFO = "doc_info"
RENDER_PAGE = "render_page"
RENDER_THUMBNAIL = "render_thumbnail"
EXTRACT_REGIONS = "extract_regions"
EXTRACT_PAGE_TEXT = "extract_page_text"
SEARCH = "search"
PREPARE_EDIT = "prepare_edit"
COMMIT_EDIT = "commit_edit"
UNDO = "undo"
REDO = "redo"
SAVE = "save"
SNAPSHOT = "snapshot"  # recovery: current doc bytes + revision
SHUTDOWN = "shutdown"


@dataclass
class Request:
    kind: str
    request_id: int
    doc_id: str | None = None
    revision: int | None = None  # client's known revision (staleness check)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    request_id: int
    kind: str
    doc_id: str | None = None
    revision: int | None = None
    ok: bool = True
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @staticmethod
    def failure(req: Request, error: str) -> Result:
        return Result(request_id=req.request_id, kind=req.kind, doc_id=req.doc_id,
                      revision=req.revision, ok=False, error=error)
