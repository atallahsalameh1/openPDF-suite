"""Background update check/download bridged to Qt signals (M12, decision D21).

The blocking urllib calls run in daemon threads (application/update_service
owns no PDF state and never touches the worker process); results arrive via
Qt signals, which are queued cross-thread, so the UI thread never blocks.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .. import __version__
from ..infrastructure import updater
from ..infrastructure.logging import get_logger

log = get_logger("updates")

CHECK_INTERVAL_DAYS = 1


class UpdateService(QObject):
    """Check GitHub Releases and download an installer, off the UI thread."""

    checked = Signal(object)   # payload dict when an update exists, else None
    failed = Signal(str)       # user-facing error (manual checks surface it)
    download_progress = Signal(int, int)  # done, total (-1 when unknown)
    downloaded = Signal(dict)  # {"path", "verified", "tag", "page"}

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._cancel = threading.Event()

    # -- check ---------------------------------------------------------------
    def check(self, quiet: bool = True) -> None:
        """Look up the latest release; `quiet` swallows network failures."""

        def work():
            try:
                release = updater.fetch_latest_release()
            except Exception as exc:  # offline / rate-limited / API change
                log.info("update check failed: %s", exc.__class__.__name__)
                if quiet:
                    self.checked.emit(None)
                else:
                    self.failed.emit(
                        "Could not reach GitHub to check for updates "
                        f"({exc.__class__.__name__}). Check your internet "
                        "connection and try again.")
                return
            tag = release.get("tag", "")
            if not updater.is_newer(tag, __version__):
                log.info("up to date (latest %s, running %s)", tag, __version__)
                self.checked.emit(None)
                return
            asset = updater.pick_setup_asset(release.get("assets", []))
            if asset is None:
                log.info("release %s has no installer asset", tag)
                self.checked.emit(None)
                return
            self.checked.emit({
                "tag": tag,
                "notes": release.get("notes", ""),
                "page": release.get("page", ""),
                "exe_url": asset["exe"].get("browser_download_url", ""),
                "sha_url": (asset["sha256"] or {}).get("browser_download_url", ""),
                "size": int(asset["exe"].get("size", 0)),
            })

        threading.Thread(target=work, daemon=True,
                         name="openpdfsuite-update-check").start()

    # -- download ------------------------------------------------------------
    def download(self, payload: dict, update_dir: Path) -> None:
        """Download the installer (+ optional checksum sidecar) to update_dir."""
        self._cancel.clear()
        url = payload.get("exe_url", "")
        if not url:
            self.failed.emit("The release has no installer to download.")
            return
        exe_name = url.rsplit("/", 1)[-1]

        def work():
            try:
                def progress(done: int, total: int | None) -> None:
                    if self._cancel.is_set():
                        raise updater.UpdateError("Download canceled.")
                    self.download_progress.emit(done, total if total else -1)

                dest = update_dir / exe_name
                updater.download_to(url, dest, progress)
                if self._cancel.is_set():
                    self.downloaded.emit({"path": "", "verified": False,
                                          "tag": payload.get("tag", ""),
                                          "page": payload.get("page", "")})
                    return
                expected = ""
                sha_url = payload.get("sha_url", "")
                if sha_url:
                    sidecar = update_dir / (exe_name + ".sha256")
                    updater.download_to(sha_url, sidecar)
                    expected = updater.sha256_of_hash_file(
                        sidecar.read_text(encoding="utf-8", errors="replace"))
                verified = False
                if expected:
                    verified = updater.verify_sha256(dest, expected)
                    if not verified:
                        self.downloaded.emit({"path": "", "verified": False,
                                              "mismatch": True,
                                              "tag": payload.get("tag", ""),
                                              "page": payload.get("page", "")})
                        return
                self.downloaded.emit({"path": str(dest), "verified": verified,
                                      "tag": payload.get("tag", ""),
                                      "page": payload.get("page", "")})
            except Exception as exc:
                if isinstance(exc, updater.UpdateError) and \
                        "canceled" in str(exc).lower():
                    log.info("update download canceled")
                    self.downloaded.emit({"path": "", "verified": False,
                                          "tag": payload.get("tag", ""),
                                          "page": payload.get("page", "")})
                    return
                log.warning("update download failed: %s", exc.__class__.__name__)
                self.failed.emit(
                    "The download failed "
                    f"({exc.__class__.__name__}). Try again, or download the "
                    "installer from the release page manually.")

        threading.Thread(target=work, daemon=True,
                         name="openpdfsuite-update-download").start()

    def cancel_download(self) -> None:
        self._cancel.set()
