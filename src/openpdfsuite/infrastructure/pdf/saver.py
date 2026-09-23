"""Validated, atomic saving (AGENTS.md §12, decision D8).

Pipeline: write candidate temp file on the destination filesystem -> reopen ->
structural checks (page count, changed pages render + contain expected text)
-> atomic `os.replace` -> original stays intact on any failure. Always a full
rewrite, never incremental (incremental saves retain earlier text revisions).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf


class SaveError(Exception):
    """User-facing save failure; message must be actionable."""


@dataclass
class SaveChecks:
    """Expected outcomes used to validate the written candidate."""

    page_count: int
    changed_pages: set[int] = field(default_factory=set)
    expected_text: dict[int, str] = field(default_factory=dict)  # page -> text that must appear
    forbidden_text: dict[int, str] = field(default_factory=dict)  # page -> text that must be gone


def fingerprint_file(path: str | Path, chunk: int = 65536) -> str:
    """size + mtime + sha1 of first chunk — cheap staleness fingerprint (§11)."""
    p = Path(path)
    st = p.stat()
    h = hashlib.sha1()
    with open(p, "rb") as f:
        h.update(f.read(chunk))
    return f"{st.st_size}-{int(st.st_mtime)}-{h.hexdigest()[:16]}"


def has_signature(doc: pymupdf.Document) -> bool:
    if getattr(doc, "is_signed", False):
        return True
    try:
        sig_type = getattr(pymupdf, "PDF_WIDGET_TYPE_SIGNATURE", 12)
        for page in doc:
            for w in page.widgets() or []:
                if getattr(w, "field_type", None) == sig_type:
                    return True
    except Exception:
        return False
    return False


def save_validated(
    doc: pymupdf.Document,
    dest: str | Path,
    checks: SaveChecks,
    encryption: str = "keep",  # "keep" | "remove" — never silently chosen
    was_encrypted: bool | None = None,
) -> Path:
    """Full-rewrite save with candidate validation and atomic replace.

    `was_encrypted` must reflect the file at open time: PyMuPDF clears
    `doc.is_encrypted` once authenticated, so relying on it here would
    silently drop encryption (§12).
    """
    dest = Path(dest)
    if dest.exists() and not os.access(dest, os.W_OK):
        raise SaveError(f"Destination is not writable: {dest.name}")
    encrypted = bool(was_encrypted) if was_encrypted is not None else doc.is_encrypted
    if encrypted:
        if encryption == "keep":
            try:
                keep = pymupdf.PDF_ENCRYPT_KEEP
            except AttributeError as exc:
                raise SaveError(
                    "This PyMuPDF build cannot preserve encryption. Choose an "
                    "explicit output option."
                ) from exc
        else:
            keep = pymupdf.PDF_ENCRYPT_NONE
    else:
        keep = pymupdf.PDF_ENCRYPT_NONE

    free = _free_space(dest.parent)
    if free is not None and free < 64 * 1024 * 1024:
        raise SaveError("Less than 64 MB of free disk space on the destination drive.")

    fd, tmp_name = tempfile.mkstemp(suffix=".openpdfsuite-tmp", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        doc.save(str(tmp), garbage=0, deflate=True, clean=False, encryption=keep)
        _validate_candidate(tmp, checks)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def _validate_candidate(tmp: Path, checks: SaveChecks) -> None:
    try:
        check = pymupdf.open(str(tmp))
    except Exception as exc:
        raise SaveError(f"Saved file failed to reopen ({exc}). Original kept.") from exc
    try:
        if check.page_count != checks.page_count:
            raise SaveError(
                f"Page count mismatch after save ({check.page_count} != "
                f"{checks.page_count}). Original kept."
            )
        for p in sorted(checks.changed_pages)[:8]:
            if p >= check.page_count:
                raise SaveError(f"Changed page {p} missing in saved file.")
            page = check[p]
            page.get_pixmap(dpi=36)  # must render without error
            text = page.get_text()
            want = checks.expected_text.get(p)
            if want and want not in " ".join(text.split()):
                raise SaveError(
                    f"Expected edited text missing on page {p + 1}. Original kept."
                )
            gone = checks.forbidden_text.get(p)
            if gone and gone in " ".join(text.split()):
                raise SaveError(
                    f"Removed text still present on page {p + 1}. Original kept."
                )
    finally:
        check.close()


def _free_space(directory: Path) -> int | None:
    try:
        usage = os.statvfs(directory)  # not on Windows
        return usage.f_bavail * usage.f_frsize
    except (AttributeError, OSError):
        try:
            import ctypes

            free = ctypes.c_ulonglong(0)
            ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(str(directory)), None,
                ctypes.c_void_p(), ctypes.byref(free)) if os.name == "nt" else 0
            return free.value if ok else None
        except Exception:
            return None
