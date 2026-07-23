"""Single-pass scan engine: evaluate every rule on every row, stream matches to SQLite."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .evaluate import compile_rules
from .rows import RowStream
from .rules import RuleSet
from .store import ResultStore

_BATCH_SIZE = 1000


@dataclass(slots=True)
class ScanProgress:
    """A progress snapshot handed to the progress callback during a scan."""

    rows_processed: int
    bytes_read: int | None
    total_bytes: int | None
    total_rows: int | None
    counts: dict[str, int]

    @property
    def fraction(self) -> float | None:
        """Progress in [0, 1], or None when the total is unknown (indeterminate)."""
        if self.total_bytes:
            return min(1.0, (self.bytes_read or 0) / self.total_bytes)
        if self.total_rows:
            return min(1.0, self.rows_processed / self.total_rows)
        return None


@dataclass(frozen=True, slots=True)
class ScanSummary:
    """Final outcome of a scan; ``cancelled`` marks partial results."""

    rows_scanned: int
    counts: dict[str, int]
    cancelled: bool
    elapsed_seconds: float


ProgressCallback = Callable[[ScanProgress], None]


def run_scan(
    stream: RowStream,
    ruleset: RuleSet,
    store: ResultStore,
    *,
    progress_callback: ProgressCallback | None = None,
    progress_interval_rows: int = 2000,
    cancel: threading.Event | None = None,
) -> ScanSummary:
    """Scan ``stream`` once, evaluating all rules of ``ruleset`` on each row.

    Matches are appended to ``store`` in batches.  ``progress_callback`` is
    invoked every ``progress_interval_rows`` rows and once at the end; it runs
    on the scanning thread, so GUI callers must forward it through a queue.
    Setting ``cancel`` aborts cleanly after the current row, keeping the
    matches found so far (the summary is then marked ``cancelled``).

    Raises :class:`lsa.core.columns.ColumnResolutionError` when a rule
    references a column the file does not have.
    """
    started = time.perf_counter()
    counts: dict[str, int] = {rule.id: 0 for rule in ruleset.rules}

    def snapshot(rows_processed: int) -> ScanProgress:
        return ScanProgress(
            rows_processed=rows_processed,
            bytes_read=stream.bytes_read(),
            total_bytes=stream.total_bytes(),
            total_rows=stream.total_rows,
            counts=dict(counts),
        )

    if stream.width == 0:  # empty file: nothing to resolve columns against
        if progress_callback is not None:
            progress_callback(snapshot(0))
        return ScanSummary(0, counts, cancelled=False, elapsed_seconds=0.0)

    compiled = compile_rules(ruleset, stream.header, stream.width)
    cache_cells = not stream.supports_offsets

    rows_processed = 0
    cancelled = False
    batch: list[tuple[str, int, int | None, str | None]] = []
    for row in stream.rows():
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        rows_processed += 1
        matched = compiled.evaluate(row.cells)
        if matched:
            cells_json = json.dumps(row.cells, ensure_ascii=False) if cache_cells else None
            for rule_id in matched:
                counts[rule_id] += 1
                batch.append((rule_id, row.number, row.offset, cells_json))
            if len(batch) >= _BATCH_SIZE:
                store.add_matches(batch)
                batch.clear()
        if (
            progress_callback is not None
            and progress_interval_rows
            and rows_processed % progress_interval_rows == 0
        ):
            progress_callback(snapshot(rows_processed))

    if batch:
        store.add_matches(batch)
    store.flush()
    if progress_callback is not None:
        progress_callback(snapshot(rows_processed))
    return ScanSummary(
        rows_scanned=rows_processed,
        counts=counts,
        cancelled=cancelled,
        elapsed_seconds=time.perf_counter() - started,
    )
