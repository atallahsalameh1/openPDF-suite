"""Application settings (QSettings-backed, per-user on Windows).

Never store document contents or passwords here — only paths, UI state, and
preferences (AGENTS.md §16).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths

from ..domain.models import DocxExportOptions

ORG = "OpenPDFSuite"
APP = "openPDF suite"
MAX_RECENTS = 10


def app_data_dir() -> Path:
    """%APPDATA%/OpenPDFSuite — recovery data lives here (AGENTS.md §11)."""
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    p = Path(base) if base else Path.home() / "AppData" / "Roaming" / ORG
    p.mkdir(parents=True, exist_ok=True)
    return p


def app_cache_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
    p = Path(base) if base else Path.home() / "AppData" / "Local" / ORG / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


class Settings:
    """Typed facade over QSettings."""

    def __init__(self) -> None:
        self._q = QSettings(ORG, APP)

    # -- theme -----------------------------------------------------------
    @property
    def theme(self) -> str:  # "light" | "dark" | "system"
        return str(self._q.value("ui/theme", "system"))

    @theme.setter
    def theme(self, value: str) -> None:
        self._q.setValue("ui/theme", value)

    @property
    def reduced_motion(self) -> bool:
        return str(self._q.value("ui/reduced_motion", "false")).lower() == "true"

    @reduced_motion.setter
    def reduced_motion(self, value: bool) -> None:
        self._q.setValue("ui/reduced_motion", "true" if value else "false")

    # -- recents ------------------------------------------------------------
    def recent_files(self) -> list[str]:
        raw = self._q.value("files/recent", [])
        if isinstance(raw, str):
            raw = [raw] if raw else []
        return [str(p) for p in (raw or [])]

    def push_recent(self, path: str) -> None:
        p = str(Path(path).resolve())
        recents = [r for r in self.recent_files() if r.lower() != p.lower()]
        recents.insert(0, p)
        self._q.setValue("files/recent", recents[:MAX_RECENTS])

    def remove_recent(self, path: str) -> None:
        p = str(Path(path).resolve()).lower()
        self._q.setValue(
            "files/recent", [r for r in self.recent_files() if r.lower() != p]
        )

    def clear_recents(self) -> None:
        self._q.setValue("files/recent", [])

    # -- dialogs -----------------------------------------------------------
    @property
    def last_open_dir(self) -> str:
        return str(self._q.value("files/last_open_dir", str(Path.home())))

    @last_open_dir.setter
    def last_open_dir(self, value: str) -> None:
        self._q.setValue("files/last_open_dir", value)

    # -- docx export options ---------------------------------------------------
    def docx_options(self) -> DocxExportOptions:
        """User-tunable PDF→DOCX options. Defaults match the v1 plan."""
        def _b(key: str, default: bool) -> bool:
            return str(self._q.value(f"docx/{key}", "true" if default else "false")
                        ).lower() == "true"
        return DocxExportOptions(
            embed_images=_b("embed_images", True),
            detect_tables=_b("detect_tables", True),
            detect_columns=_b("detect_columns", True),
            flow_mode=str(self._q.value("docx/flow_mode", "formatted")),
        )

    def set_docx_option(self, key: str, value) -> None:
        self._q.setValue(f"docx/{key}", "true" if value else "false"
                         if isinstance(value, bool) else str(value))

    # -- window geometry ------------------------------------------------------
    def save_geometry(self, key: str, data) -> None:
        self._q.setValue(f"window/{key}/geometry", data)

    def restore_geometry(self, key: str):
        return self._q.value(f"window/{key}/geometry")

    def save_state(self, key: str, data) -> None:
        self._q.setValue(f"window/{key}/state", data)

    def restore_state(self, key: str):
        return self._q.value(f"window/{key}/state")

    def sync(self) -> None:
        self._q.sync()
