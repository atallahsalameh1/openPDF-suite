"""GitHub Releases update check + download (M12, decision D21).

Pure stdlib (urllib + hashlib) so the logic is headless-testable and runs
inside plain worker threads; the application layer (application/
update_service.py) bridges results to Qt signals. Never imported by the PDF
worker process.

Integrity policy: releases built with build.bat carry an optional
`OpenPDFSuiteSetup-<ver>.exe.sha256` sidecar. When present, the downloaded
installer is verified before it may run; when absent, the UI warns and the
user decides explicitly — verification-less installs are never silent.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.request
from pathlib import Path

GITHUB_REPO = "atallahsalameh1/openPDF-suite"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
SETUP_ASSET_PREFIX = "OpenPDFSuiteSetup"
_UA = "openpdfsuite-updater"


class UpdateError(Exception):
    """User-facing update failure; message must be actionable."""


def parse_tag(tag: str) -> tuple[int, ...]:
    """`'v0.2.1'` -> `(2, 1)`. Non-numeric parts count as 0."""
    parts: list[int] = []
    for piece in tag.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest_tag: str, current: str) -> bool:
    return parse_tag(latest_tag) > parse_tag(current)


def pick_setup_asset(assets: list[dict]) -> dict | None:
    """Pick the installer asset (`OpenPDFSuiteSetup-<ver>.exe`) plus its
    optional `.sha256` sidecar from a release's asset list."""
    setup = None
    sha = None
    for asset in assets:
        name = str(asset.get("name", ""))
        if name.startswith(SETUP_ASSET_PREFIX) and name.endswith(".exe"):
            setup = asset
        elif name.startswith(SETUP_ASSET_PREFIX) and name.endswith(".exe.sha256"):
            sha = asset
    if setup is None:
        return None
    return {"exe": setup, "sha256": sha}


def sha256_of_hash_file(text: str) -> str:
    """Extract the hex digest from a .sha256 sidecar (bare hash, or
    `<hash> <filename>` / certutil-style). Empty when not parseable."""
    token = text.strip().split()[0] if text.strip() else ""
    if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token):
        return token.lower()
    return ""


def fetch_latest_release(timeout: float = 10.0) -> dict:
    """Query the GitHub API for the latest release.

    Returns {tag, name, notes, page, assets}. Raises on network/HTTP errors.
    """
    req = urllib.request.Request(
        RELEASES_API,
        headers={"User-Agent": _UA, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return {
        "tag": str(data.get("tag_name", "")),
        "name": str(data.get("name") or data.get("tag_name", "")),
        "notes": str(data.get("body") or ""),
        "page": str(data.get("html_url", "")),
        "assets": list(data.get("assets") or []),
    }


def download_to(url: str, dest: Path, progress=None,
                timeout: float = 30.0) -> Path:
    """Stream `url` to `dest` (`.part` temp first, atomic rename at the end).

    `progress(done_bytes, total_bytes_or_None)` is called per chunk; raising
    inside it aborts the download and removes the temp file.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, \
                open(tmp, "wb") as fh:
            header = resp.headers.get("Content-Length")
            total = int(header) if header and header.isdigit() else None
            done = 0
            while True:
                chunk = resp.read(1 << 17)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
        if total is not None and done != total:
            raise UpdateError(
                f"Download truncated ({done} of {total} bytes received).")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)
    return dest


def verify_sha256(path: Path, expected_hex: str) -> bool:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().lower() == expected_hex.strip().lower()


def spawn_installer(setup_path: Path) -> None:
    """Launch the Inno Setup installer detached from this (dying) process.

    `/closeapplications` lets Inno close a still-running copy; the caller
    quits right after spawning so the installer owns the machine.
    """
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [str(setup_path), "/closeapplications", "/restartapplications"],
        close_fds=True, creationflags=flags)
