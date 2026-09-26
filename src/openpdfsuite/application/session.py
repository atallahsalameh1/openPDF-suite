"""UI-side document session state (AGENTS.md §8: DocumentSession).

The authoritative document lives in the worker; this tracks what the window
needs: identity, revision, dirty state, undo/redo availability, page geometry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.models import DocumentMeta


@dataclass
class DocumentSession:
    path: Path | None
    doc_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    meta: DocumentMeta | None = None
    page_sizes: list[tuple[float, float]] = field(default_factory=list)
    page_rotations: list[int] = field(default_factory=list)  # per-page /Rotate (0/90/180/270)
    revision: int = 0
    saved_revision: int = 0  # revision at last successful save
    dirty: bool = False
    can_undo: bool = False
    can_redo: bool = False
    current_page: int = 0
    zoom: float = 1.0
    fingerprint: str = ""
    source_changed_on_disk: bool = False

    @property
    def page_count(self) -> int:
        return len(self.page_sizes)

    @property
    def display_name(self) -> str:
        return self.path.name if self.path else "Untitled"

    def mark_committed(self, revision: int, page: int) -> None:
        self.revision = revision
        self.dirty = True
        self.can_undo = True
        self.can_redo = False
        self.current_page = page

    def mark_saved(self, fingerprint: str) -> None:
        self.saved_revision = self.revision
        self.dirty = False
        self.fingerprint = fingerprint

    def mark_undo_redo(self, revision: int, can_undo: bool, can_redo: bool,
                       page: int) -> None:
        self.revision = revision
        self.can_undo = can_undo
        self.can_redo = can_redo
        self.dirty = self.revision != self.saved_revision
        self.current_page = page
