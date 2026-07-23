"""Scan worker thread for the GUI.

tkinter widgets must only be touched from the main thread, so the scan runs
in a daemon thread and communicates exclusively through a queue.  The view
polls :meth:`ScanController.drain` from an ``after()`` loop.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from pathlib import Path
from typing import NamedTuple

from lsa.core.csv_stream import CsvOptions
from lsa.core.rules import RuleSet
from lsa.core.scan import run_scan
from lsa.core.store import ResultStore
from lsa.core.streams import open_stream


class ScanMessage(NamedTuple):
    """Message from the worker: kind is 'progress', 'done' or 'error'."""

    kind: str
    payload: object


class ScanController:
    """Owns the worker thread, the cancel flag and the scan results."""

    def __init__(self) -> None:
        self.queue: queue.Queue[ScanMessage] = queue.Queue()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.store: ResultStore | None = None
        self.summary = None
        self.header: list[str] | None = None
        self.width = 0
        self.sheet_name: str | None = None
        self._config: tuple | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        *,
        path: Path,
        ruleset: RuleSet,
        csv_options: CsvOptions | None,
        sheet: str | None,
        has_header: bool,
    ) -> None:
        """Launch the scan in a worker thread (one scan at a time)."""
        if self.running:
            raise RuntimeError("a scan is already running")
        self.discard_results()
        self.drain()  # drop stale messages from any previous (orphaned) scan
        self._config = self._config_key(path, ruleset, csv_options, sheet, has_header)
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._work,
            args=(path, ruleset, csv_options, sheet, has_header),
            daemon=True,
            name="lsa-scan",
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def drain(self) -> list[ScanMessage]:
        """All messages queued since the last call (never blocks)."""
        messages = []
        while True:
            try:
                messages.append(self.queue.get_nowait())
            except queue.Empty:
                return messages

    def join(self, timeout: float | None = None) -> None:
        """Wait for the worker to finish (used by tests and shutdown)."""
        if self._thread is not None:
            self._thread.join(timeout)

    def discard_results(self) -> None:
        """Close and forget the previous scan's result store."""
        if self.store is not None:
            self.store.close()
            self.store = None
        self.summary = None
        self._config = None

    @staticmethod
    def _config_key(
        path: Path,
        ruleset: RuleSet,
        csv_options: CsvOptions | None,
        sheet: str | None,
        has_header: bool,
    ) -> tuple:
        return (str(path), sheet, has_header, csv_options, ruleset)

    def results_valid_for(
        self,
        *,
        path: Path,
        ruleset: RuleSet,
        csv_options: CsvOptions | None,
        sheet: str | None,
        has_header: bool,
    ) -> bool:
        """Whether the stored results were produced by exactly this configuration.

        Prevents a report built for file A / rules A from being shown after
        the user went Back and changed the file or the rules.
        """
        return (
            self.summary is not None
            and self.store is not None
            and self._config == self._config_key(path, ruleset, csv_options, sheet, has_header)
        )

    def _work(
        self,
        path: Path,
        ruleset: RuleSet,
        csv_options: CsvOptions | None,
        sheet: str | None,
        has_header: bool,
    ) -> None:
        store = ResultStore()
        stream = None
        try:
            stream = open_stream(
                path, csv_options=csv_options, sheet=sheet, has_header=has_header
            )
            summary = run_scan(
                stream,
                ruleset,
                store,
                progress_callback=lambda p: self.queue.put(ScanMessage("progress", p)),
                progress_interval_rows=5000,
                cancel=self._cancel,
            )
            self.header = stream.header
            self.width = stream.width
            self.sheet_name = getattr(stream, "sheet_name", None)
            self.store = store
            self.summary = summary
            self.queue.put(ScanMessage("done", summary))
        except BaseException as exc:
            # BaseException: python-calamine's Rust panics (pyo3
            # PanicException) bypass `except Exception`; a dead worker that
            # never posts a message would hang the GUI in "running" forever.
            store.close()
            self.queue.put(ScanMessage("error", str(exc) or repr(exc)))
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
