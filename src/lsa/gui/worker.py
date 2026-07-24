"""Scan worker for the GUI: the scan runs in a separate *process*.

Tcl/Tk is not thread-safe, and even indirect interactions can be fatal: with
an in-process worker thread, Python's cyclic GC can run on the scanning
thread and finalize leftover Tk objects (variables/commands of rebuilt
wizard steps), calling into Tcl from the wrong thread and aborting the whole
application mid-scan.  Running the scan in a spawned subprocess makes that
class of crash structurally impossible — no Tk exists in the scan process —
and has a second benefit: a scan killed by the OS (e.g. OOM) no longer takes
the window down; the controller detects the dead process and reports it.

The UI talks to the controller exclusively through :meth:`ScanController.drain`,
polled from an ``after()`` loop on the main thread.
"""

from __future__ import annotations

import multiprocessing
import queue as queue_module
from pathlib import Path
from typing import NamedTuple

from lsa.core.csv_stream import CsvOptions
from lsa.core.rules import RuleSet
from lsa.core.scan import run_scan
from lsa.core.store import ResultStore, make_temp_store_path
from lsa.core.streams import open_stream

# spawn (never fork): a forked child would inherit the parent's Tcl/Tk state,
# re-creating exactly the unsafety this design removes; spawn is also the
# only start method available everywhere (Windows/macOS/Linux).
_CTX = multiprocessing.get_context("spawn")


class ScanMessage(NamedTuple):
    """Message from the scan process: kind is 'progress', 'done' or 'error'."""

    kind: str
    payload: object


def _scan_process(
    message_queue,
    cancel_event,
    db_path: str,
    path: str,
    ruleset: RuleSet,
    csv_options: CsvOptions | None,
    sheet: str | None,
    has_header: bool,
) -> None:
    """Entry point of the scan subprocess (top-level for spawn picklability)."""
    store = ResultStore(db_path)  # caller-provided path: the parent owns the file
    stream = None
    try:
        stream = open_stream(path, csv_options=csv_options, sheet=sheet, has_header=has_header)
        summary = run_scan(
            stream,
            ruleset,
            store,
            progress_callback=lambda p: message_queue.put(ScanMessage("progress", p)),
            progress_interval_rows=5000,
            cancel=cancel_event,
        )
        payload = (summary, stream.header, stream.width, getattr(stream, "sheet_name", None))
        message_queue.put(ScanMessage("done", payload))
    except BaseException as exc:
        message_queue.put(ScanMessage("error", str(exc) or repr(exc)))
    finally:
        if stream is not None:
            stream.close()
        store.close()


class ScanController:
    """Owns the scan subprocess, the cancel flag and the scan results."""

    def __init__(self) -> None:
        self._queue = _CTX.Queue()
        self._cancel = _CTX.Event()
        self._process: multiprocessing.process.BaseProcess | None = None
        self._db_path: Path | None = None
        self._config: tuple | None = None
        self.store: ResultStore | None = None
        self.summary = None
        self.header: list[str] | None = None
        self.width = 0
        self.sheet_name: str | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def start(
        self,
        *,
        path: Path,
        ruleset: RuleSet,
        csv_options: CsvOptions | None,
        sheet: str | None,
        has_header: bool,
    ) -> None:
        """Launch the scan subprocess (one scan at a time)."""
        if self.running:
            raise RuntimeError("a scan is already running")
        self.discard_results()
        self.drain()  # drop stale messages from any previous scan
        self._config = self._config_key(path, ruleset, csv_options, sheet, has_header)
        self._cancel.clear()
        self._db_path = make_temp_store_path()
        self._process = _CTX.Process(
            target=_scan_process,
            args=(
                self._queue,
                self._cancel,
                str(self._db_path),
                str(path),
                ruleset,
                csv_options,
                sheet,
                has_header,
            ),
            daemon=True,
            name="lsa-scan",
        )
        self._process.start()

    def cancel(self) -> None:
        self._cancel.set()

    def drain(self) -> list[ScanMessage]:
        """All messages received since the last call (never blocks).

        Terminal messages update the controller state: on 'done' the result
        store is opened in this process.  If the scan process died without
        reporting (segfault, OOM kill...), a synthetic 'error' is emitted so
        the UI never hangs on a scan that will never finish.
        """
        process = self._process
        was_alive = process.is_alive() if process is not None else False
        messages: list[ScanMessage] = []
        while True:
            try:
                message = self._queue.get_nowait()
            except queue_module.Empty:
                break
            if message.kind == "done":
                summary, header, width, sheet_name = message.payload
                self.summary = summary
                self.header = header
                self.width = width
                self.sheet_name = sheet_name
                if self._db_path is not None:
                    self.store = ResultStore(self._db_path)
                message = ScanMessage("done", summary)
            messages.append(message)

        if (
            process is not None
            and not was_alive
            and self.summary is None
            and not any(m.kind in ("done", "error") for m in messages)
        ):
            exitcode = process.exitcode
            self._process = None
            messages.append(
                ScanMessage(
                    "error",
                    f"the scan process terminated unexpectedly (exit code {exitcode}); "
                    "the system may have stopped it (out of memory?)",
                )
            )
        return messages

    def join(self, timeout: float | None = None) -> None:
        """Wait for the scan process to finish (tests and shutdown)."""
        if self._process is not None:
            self._process.join(timeout)

    def discard_results(self) -> None:
        """Close and delete the previous scan's result store."""
        if self.store is not None:
            self.store.close()
            self.store = None
        if self._db_path is not None:
            self._db_path.unlink(missing_ok=True)
            self._db_path = None
        self.summary = None
        self._config = None

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel, stop the scan process and clean up (application exit)."""
        self.cancel()
        if self._process is not None:
            self._process.join(timeout)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(1.0)
            self._process = None
        self.discard_results()

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
