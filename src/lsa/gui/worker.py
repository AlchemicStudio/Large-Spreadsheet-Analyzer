"""Background jobs for the GUI: each runs in a separate *process*.

Tcl/Tk is not thread-safe, and even indirect interactions can be fatal: with
an in-process worker thread, Python's cyclic GC can run on the worker thread
and finalize leftover Tk objects (variables/commands of rebuilt wizard
steps), calling into Tcl from the wrong thread and aborting the whole
application mid-job.  Running jobs in spawned subprocesses makes that class
of crash structurally impossible — no Tk exists in the job process — and has
a second benefit: a job killed by the OS (e.g. OOM) no longer takes the
window down; the controller detects the dead process and reports it.

The UI talks to a controller exclusively through :meth:`ProcessController.drain`,
polled from an ``after()`` loop on the main thread.
"""

from __future__ import annotations

import multiprocessing
import queue as queue_module
from pathlib import Path
from typing import Any, NamedTuple

from lsa.core.csv_stream import CsvOptions
from lsa.core.extract import extract_duplicates
from lsa.core.rules import RuleSet
from lsa.core.scan import run_scan
from lsa.core.store import ResultStore, make_temp_store_path
from lsa.core.streams import open_stream

# spawn (never fork): a forked child would inherit the parent's Tcl/Tk state,
# re-creating exactly the unsafety this design removes; spawn is also the
# only start method available everywhere (Windows/macOS/Linux).
_CTX = multiprocessing.get_context("spawn")


class ScanMessage(NamedTuple):
    """Message from a job process: kind is 'progress', 'done' or 'error'."""

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


def _extract_process(message_queue, cancel_event, kwargs: dict) -> None:
    """Entry point of the duplicate-extraction subprocess."""
    try:
        stats = extract_duplicates(
            progress_callback=lambda phase, done, total: message_queue.put(
                ScanMessage("progress", (phase, done, total))
            ),
            cancel=cancel_event,
            **kwargs,
        )
        message_queue.put(ScanMessage("done", stats))
    except BaseException as exc:
        message_queue.put(ScanMessage("error", str(exc) or repr(exc)))


class ProcessController:
    """Owns one job subprocess, its cancel flag and its message queue."""

    job_name = "job"

    def __init__(self) -> None:
        self._queue = _CTX.Queue()
        self._cancel = _CTX.Event()
        self._process: multiprocessing.process.BaseProcess | None = None
        self._terminal_seen = False

    @property
    def running(self) -> bool:
        # A job is over the moment its terminal message is absorbed - the
        # child may need a few more milliseconds to tear down, and UI state
        # (e.g. re-enabling buttons) must not race that window.
        return self._process is not None and self._process.is_alive() and not self._terminal_seen

    def _discard_queue(self) -> None:
        """Throw away pending messages without interpreting them.

        Used before starting a new run: a stale 'done' from an orphaned
        previous run must never be absorbed as if it belonged to the new one.
        """
        while True:
            try:
                self._queue.get_nowait()
            except queue_module.Empty:
                return

    def _start_process(self, target, args: tuple) -> None:
        if self.running:
            raise RuntimeError(f"a {self.job_name} is already running")
        if self._process is not None:
            self._process.join(timeout=1.0)  # reap the finished predecessor
        self._discard_queue()
        self._terminal_seen = False
        self._cancel.clear()
        self._process = _CTX.Process(
            target=target,
            args=(self._queue, self._cancel, *args),
            daemon=True,
            name=f"lsa-{self.job_name}",
        )
        self._process.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _handle_done(self, payload: object) -> object:
        """Hook: absorb a 'done' payload, return what the UI should see."""
        return payload

    def drain(self) -> list[ScanMessage]:
        """All messages received since the last call (never blocks).

        Terminal messages update the controller state via :meth:`_handle_done`.
        If the process died without reporting (segfault, OOM kill...), a
        synthetic 'error' is emitted so the UI never hangs on a job that
        will never finish.
        """
        process = self._process
        was_alive = process.is_alive() if process is not None else False
        messages: list[ScanMessage] = []
        saw_terminal = False
        while True:
            try:
                message = self._queue.get_nowait()
            except queue_module.Empty:
                break
            if message.kind == "done":
                message = ScanMessage("done", self._handle_done(message.payload))
                saw_terminal = True
            elif message.kind == "error":
                saw_terminal = True
            messages.append(message)
        if saw_terminal:
            self._terminal_seen = True

        if process is not None and not was_alive and not saw_terminal and not self._terminal_seen:
            exitcode = process.exitcode
            self._process = None
            messages.append(
                ScanMessage(
                    "error",
                    f"the {self.job_name} process terminated unexpectedly "
                    f"(exit code {exitcode}); the system may have stopped it "
                    "(out of memory?)",
                )
            )
        return messages

    def join(self, timeout: float | None = None) -> None:
        """Wait for the job process to finish (tests and shutdown)."""
        if self._process is not None:
            self._process.join(timeout)

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel, stop the job process and clean up (application exit)."""
        self.cancel()
        if self._process is not None:
            self._process.join(timeout)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(1.0)
            self._process = None
        self._cleanup()

    def _cleanup(self) -> None:
        """Hook: release job-specific resources on shutdown."""


class ScanController(ProcessController):
    """Runs the file scan and owns its results."""

    job_name = "scan"

    def __init__(self) -> None:
        super().__init__()
        self._db_path: Path | None = None
        self._config: tuple | None = None
        self.store: ResultStore | None = None
        self.summary = None
        self.header: list[str] | None = None
        self.width = 0
        self.sheet_name: str | None = None

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
            raise RuntimeError(f"a {self.job_name} is already running")
        self.discard_results()
        self._config = self._config_key(path, ruleset, csv_options, sheet, has_header)
        self._db_path = make_temp_store_path()
        self._start_process(
            _scan_process,
            (str(self._db_path), str(path), ruleset, csv_options, sheet, has_header),
        )

    def _handle_done(self, payload: object) -> object:
        summary, header, width, sheet_name = payload
        self.summary = summary
        self.header = header
        self.width = width
        self.sheet_name = sheet_name
        if self._db_path is not None:
            self.store = ResultStore(self._db_path)
        return summary

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

    def _cleanup(self) -> None:
        self.discard_results()

    @property
    def db_path(self) -> Path | None:
        """Path of the scan's SQLite store (for read-only consumers)."""
        return self._db_path

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


class ExtractController(ProcessController):
    """Runs the duplicate-file extraction and owns its output file."""

    job_name = "extraction"

    def __init__(self) -> None:
        super().__init__()
        self.stats = None
        self.out_path: Path | None = None
        self._ref_db_path: Path | None = None

    def start(self, **extract_kwargs: Any) -> None:
        """Launch the extraction subprocess; kwargs go to extract_duplicates."""
        if self.running:
            raise RuntimeError(f"a {self.job_name} is already running")
        self.discard_output()
        self.out_path = make_temp_store_path(prefix="lsa-duplicates-", suffix=".csv")
        # Parent-owned so it can be deleted even if the child is killed.
        self._ref_db_path = make_temp_store_path(prefix="lsa-refkeys-")
        extract_kwargs["out_path"] = str(self.out_path)
        extract_kwargs["ref_db_path"] = str(self._ref_db_path)
        self._start_process(_extract_process, (extract_kwargs,))

    def _handle_done(self, payload: object) -> object:
        self.stats = payload
        return payload

    def discard_output(self) -> None:
        """Delete the previous extraction's output and working files."""
        if self.out_path is not None:
            self.out_path.unlink(missing_ok=True)
            self.out_path = None
        if self._ref_db_path is not None:
            self._ref_db_path.unlink(missing_ok=True)
            self._ref_db_path = None
        self.stats = None

    def _cleanup(self) -> None:
        self.discard_output()
