"""UI-side client for the PDF worker process.

A dedicated QThread drains the result pipe so the UI thread never blocks.
Sends are lock-guarded; the pipe is duplex (parent sends requests and receives
results on the same connection object from different threads).
"""

from __future__ import annotations

import itertools
import threading

from PySide6.QtCore import QObject, QThread, Signal

from ..infrastructure.logging import get_logger
from ..infrastructure.pdf.protocol import SHUTDOWN, Request
from ..infrastructure.pdf.worker import spawn_worker

log = get_logger("worker_client")


class _Reader(QObject):
    """Runs in its own thread; emits results; detects worker death."""

    result = Signal(object)  # Result
    died = Signal(str)
    stopped = Signal()

    def __init__(self, conn, proc):
        super().__init__()
        self._conn = conn
        self._proc = proc
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        while self._running:
            try:
                if not self._conn.poll(0.05):
                    if not self._proc.is_alive():
                        self.died.emit(f"worker exited with code {self._proc.exitcode}")
                        break
                    continue
                result = self._conn.recv()
            except (EOFError, OSError) as exc:
                if self._running:
                    self.died.emit(f"worker connection lost: {exc}")
                break
            except Exception as exc:  # malformed payload — keep the UI alive
                log.error("worker reader error: %s", exc)
                continue
            self.result.emit(result)
        self.stopped.emit()


class WorkerClient(QObject):
    """Owns the worker process; serializes sends; routes results by signal."""

    result_ready = Signal(object)  # Result
    worker_died = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._proc, self._conn = spawn_worker()
        self._send_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._thread = QThread(self)
        self._reader = _Reader(self._conn, self._proc)
        self._reader.moveToThread(self._thread)
        self._thread.started.connect(self._reader.run)
        self._reader.result.connect(self.result_ready)
        self._reader.died.connect(self._on_died)
        self._thread.start()
        self.alive = True

    def _on_died(self, message: str) -> None:
        self.alive = False
        log.error("worker died: %s", message)
        self.worker_died.emit(message)

    def send(self, kind: str, payload: dict | None = None,
             doc_id: str | None = None, revision: int | None = None) -> int:
        """Enqueue a request; returns its request_id."""
        if not self.alive:
            raise RuntimeError("PDF worker is not running")
        rid = next(self._ids)
        req = Request(kind=kind, request_id=rid, doc_id=doc_id,
                      revision=revision, payload=payload or {})
        with self._send_lock:
            self._conn.send(req)
        return rid

    def shutdown(self, timeout_ms: int = 2000) -> None:
        self._reader.stop()
        try:
            with self._send_lock:
                self._conn.send(Request(kind=SHUTDOWN, request_id=-1))
        except (BrokenPipeError, OSError):
            pass
        self._thread.quit()
        self._thread.wait(timeout_ms)
        self._proc.join(timeout_ms / 1000.0)
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(1.0)  # TerminateProcess is immediate on Windows
        if self._proc.is_alive():  # pragma: no cover - unreachable on Windows
            self._proc.kill()
            self._proc.join(1.0)
        self.alive = False
        try:
            self._conn.close()
        except OSError:
            pass
