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
    """A progress snapshot handed to the progress callback during a scan.

    ``finalizing`` is True while duplicate groups are being resolved after
    the streaming pass (no row-based progress exists for that phase).
    """

    rows_processed: int
    bytes_read: int | None
    total_bytes: int | None
    total_rows: int | None
    counts: dict[str, int]
    finalizing: bool = False

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
    """Final outcome of a scan; ``cancelled`` marks partial results.

    ``malformed_rows`` counts CSV records that could not be parsed (runaway
    quoted fields...) and were skipped so the scan could continue.
    """

    rows_scanned: int
    counts: dict[str, int]
    cancelled: bool
    elapsed_seconds: float
    malformed_rows: int = 0


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
        return ScanSummary(
            0,
            counts,
            cancelled=False,
            elapsed_seconds=0.0,
            malformed_rows=getattr(stream, "malformed_rows", 0),
        )

    compiled = compile_rules(ruleset, stream.header, stream.width)
    cache_cells = not stream.supports_offsets
    dup_rules = [rule for rule in compiled.rules if rule.dup_conds]

    rows_processed = 0
    cancelled = False
    batch: list[tuple[str, int, int | None, str | None]] = []
    dup_batch: list[tuple[str, str, int, int | None, str | None, int]] = []
    for row in stream.rows():
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        rows_processed += 1
        cells = row.cells
        matched = compiled.evaluate(cells)
        cells_json: str | None = None
        if (matched or dup_rules) and cache_cells:
            cells_json = json.dumps(cells, ensure_ascii=False)
        if matched:
            for rule_id in matched:
                counts[rule_id] += 1
                batch.append((rule_id, row.number, row.offset, cells_json))
            if len(batch) >= _BATCH_SIZE:
                store.add_matches(batch)
                batch.clear()
        for rule in dup_rules:
            # is_duplicate needs whole-file knowledge: record this row's
            # key(s) now, resolve the groups after the pass.
            perrow_ok = rule.perrow(cells) if rule.perrow is not None else True
            if rule.match == "any":
                if rule.perrow is not None and perrow_ok:
                    counts[rule.rule_id] += 1
                    batch.append((rule.rule_id, row.number, row.offset, cells_json))
                candidate = 0
            else:
                candidate = 1 if perrow_ok else 0
            for cond in rule.dup_conds:
                entry = (
                    cond.cond_id,
                    cond.key_of(cells),
                    row.number,
                    row.offset,
                    cells_json,
                    candidate,
                )
                dup_batch.append(entry)
        if len(dup_batch) >= _BATCH_SIZE:
            store.add_dup_entries(dup_batch)
            dup_batch.clear()
        if (
            progress_callback is not None
            and progress_interval_rows
            and rows_processed % progress_interval_rows == 0
        ):
            progress_callback(snapshot(rows_processed))

    if batch:
        store.add_matches(batch)
    if dup_batch:
        store.add_dup_entries(dup_batch)
    store.flush()
    if dup_rules:
        if progress_callback is not None:
            finalizing = snapshot(rows_processed)
            finalizing.finalizing = True
            progress_callback(finalizing)
        for rule in dup_rules:
            # Partial data on cancel still yields (partial) duplicate groups.
            store.resolve_duplicates(
                rule.rule_id, [cond.cond_id for cond in rule.dup_conds], rule.match
            )
            counts[rule.rule_id] = store.count(rule.rule_id)
    if progress_callback is not None:
        progress_callback(snapshot(rows_processed))
    return ScanSummary(
        rows_scanned=rows_processed,
        counts=counts,
        cancelled=cancelled,
        elapsed_seconds=time.perf_counter() - started,
        malformed_rows=getattr(stream, "malformed_rows", 0),
    )
