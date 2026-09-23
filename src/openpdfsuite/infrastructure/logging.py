"""Logging setup.

Policy (AGENTS.md §16): logs must never contain document text or passwords.
Log document *paths* and sizes for diagnostics, never content. Modules must
not log extracted text.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_CONFIGURED = False


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> Path:
    global _CONFIGURED
    if log_dir is None:
        from .settings import app_cache_dir

        log_dir = app_cache_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "openpdfsuite.log"

    root = logging.getLogger("openpdfsuite")
    if _CONFIGURED:
        return log_file
    root.setLevel(level)
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(stream)
    _CONFIGURED = True
    return log_file


class _NoContentFilter(logging.Filter):
    """Defense in depth: redact anything that looks like a password field."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for token in ("password=", "passwd=", "pwd="):
            if token in msg.lower():
                idx = msg.lower().index(token)
                record.msg = str(record.msg)[:idx] + token + "***"
                record.args = ()
        return True


def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(f"openpdfsuite.{name}")
    log.addFilter(_NoContentFilter())
    return log
