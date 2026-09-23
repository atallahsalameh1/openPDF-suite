"""Unsaved-session recovery storage (AGENTS.md §11).

Recovery data lives under %APPDATA%/OpenPDFSuite/recovery as one JSON meta file plus
one PDF byte file per session. Writes are atomic (temp + os.replace). Entries
are removed only by explicit user discard or a successful save of that session
— unresolved data is never silently deleted.

Staleness is computed against the original source file: missing or changed
sources are reported but the entry is still restorable (the recovered bytes
are independent of the source).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .logging import get_logger
from .pdf.saver import fingerprint_file
from .settings import app_data_dir

log = get_logger("recovery")

_META_SUFFIX = ".json"
_BYTES_SUFFIX = ".pdf"


@dataclass
class RecoveryEntry:
    session_id: str
    source_path: str
    fingerprint: str
    revision: int
    saved_revision: int
    timestamp: str  # ISO 8601 UTC
    stale: bool = False
    stale_reason: str = ""


class RecoveryStore:
    def __init__(self, base: Path | None = None):
        self.base = Path(base) if base else app_data_dir() / "recovery"
        self.base.mkdir(parents=True, exist_ok=True)

    # -- writing -----------------------------------------------------------
    def write(self, session_id: str, source_path: str, fingerprint: str,
              revision: int, saved_revision: int, doc_bytes: bytes) -> Path | None:
        """Atomically persist a session snapshot. Returns the bytes path."""
        if not doc_bytes:
            return None
        meta = {
            "session_id": session_id,
            "source_path": source_path,
            "fingerprint": fingerprint,
            "revision": revision,
            "saved_revision": saved_revision,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        try:
            self._atomic_write(self.base / f"{session_id}{_BYTES_SUFFIX}", doc_bytes)
            self._atomic_write(
                self.base / f"{session_id}{_META_SUFFIX}",
                json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        except OSError as exc:
            log.warning("recovery write failed for %s: %s",
                        session_id.split("-")[0], exc.__class__.__name__)
            return None
        return self.base / f"{session_id}{_BYTES_SUFFIX}"

    def _atomic_write(self, dest: Path, data: bytes) -> None:
        fd, tmp_name = tempfile.mkstemp(suffix=".tmp", dir=str(self.base))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_name, dest)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- reading -----------------------------------------------------------
    def list(self) -> list[RecoveryEntry]:
        entries: list[RecoveryEntry] = []
        for meta_path in sorted(self.base.glob(f"*{_META_SUFFIX}")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            entry = RecoveryEntry(
                session_id=meta.get("session_id", meta_path.stem),
                source_path=meta.get("source_path", ""),
                fingerprint=meta.get("fingerprint", ""),
                revision=int(meta.get("revision", 0)),
                saved_revision=int(meta.get("saved_revision", 0)),
                timestamp=meta.get("timestamp", ""),
            )
            if (self.base / f"{entry.session_id}{_BYTES_SUFFIX}").exists():
                entry.stale, entry.stale_reason = self._staleness(entry)
                entries.append(entry)
            # meta without bytes is unusable; left on disk per §11 (never
            # auto-delete) but not offered to the user
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries

    def _staleness(self, entry: RecoveryEntry) -> tuple[bool, str]:
        if not entry.source_path:
            return False, ""
        src = Path(entry.source_path)
        if not src.exists():
            return True, "the original file no longer exists"
        try:
            if entry.fingerprint and fingerprint_file(src) != entry.fingerprint:
                return True, "the original file changed since the session"
        except OSError:
            pass
        return False, ""

    def load_bytes(self, session_id: str) -> bytes | None:
        try:
            return (self.base / f"{session_id}{_BYTES_SUFFIX}").read_bytes()
        except OSError:
            return None

    # -- removal (explicit only) --------------------------------------------
    def discard(self, session_id: str) -> None:
        for suffix in (_META_SUFFIX, _BYTES_SUFFIX):
            try:
                (self.base / f"{session_id}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass
