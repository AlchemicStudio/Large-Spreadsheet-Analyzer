"""Generate a "duplicates to remove" CSV from a scan's duplicate groups.

For every duplicate group of an ``is_duplicate`` rule:

- **Case A** — rows whose right-hand cells are identical (the named
  sub-groups): the row with the lowest ID survives, every other row is
  appended to the duplicate file.
- **Case B** — rows whose right-hand cells are unique within their group:
  the row is kept only if an identical row exists in a user-chosen
  *reference file* (compared over the mapped columns); otherwise it is
  appended to the duplicate file.

Everything streams: the reference file goes into a temporary on-disk SQLite
key set, matches are read in block order from the (read-only) scan store,
and rows are re-fetched lazily — memory stays flat regardless of file sizes.
"""

from __future__ import annotations

import csv
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .csv_stream import CsvOptions
from .report import MatchRowSource
from .rules import Settings
from .store import ResultStore, make_temp_store_path
from .streams import open_stream

_KEY_SEPARATOR = "\x1f"
_BATCH_SIZE = 2000

ProgressCallback = Callable[[str, int, int | None], None]
"""(phase, done, total): phase is "reference" (rows read) or "groups"."""


@dataclass(frozen=True, slots=True)
class ColumnMapping:
    """Aligned column pairs: source file column index -> reference column index."""

    pairs: tuple[tuple[int, int], ...]

    def source_indexes(self) -> tuple[int, ...]:
        return tuple(source for source, _ref in self.pairs)

    def reference_indexes(self) -> tuple[int, ...]:
        return tuple(ref for _source, ref in self.pairs)


@dataclass(frozen=True, slots=True)
class ExtractStats:
    """Outcome of one extraction run."""

    reference_rows: int
    groups_processed: int
    kept_rows: int
    case_a_rows: int
    case_b_rows: int
    cancelled: bool = False

    @property
    def duplicates_written(self) -> int:
        return self.case_a_rows + self.case_b_rows


def _value_transform(settings: Settings) -> Callable[[str], str]:
    trim = settings.trim
    fold = None if settings.case_sensitive else str.casefold

    def transform(value: str) -> str:
        if trim:
            value = value.strip()
        return value if fold is None else fold(value)

    return transform


def _row_key(cells: Sequence[str], indexes: Sequence[int], transform: Callable[[str], str]) -> str:
    return _KEY_SEPARATOR.join(transform(cells[i]) if i < len(cells) else "" for i in indexes)


def _id_sort_key(cells: Sequence[str], id_column: int, row_number: int) -> tuple:
    """Sortable identity: numeric IDs compare numerically, others as text.

    Numeric IDs sort before non-numeric ones; ties fall back to the row
    number, keeping the earliest row deterministically.
    """
    raw = (cells[id_column] if id_column < len(cells) else "").strip()
    try:
        return (0, int(raw), "", row_number)
    except ValueError:
        try:
            value = float(raw)
            if value != value:  # NaN compares false with everything: poison
                raise ValueError(raw)
            return (0, value, "", row_number)
        except ValueError:
            return (1, 0, raw, row_number)


def _ingest_reference(
    connection: sqlite3.Connection,
    reference_path: str | Path,
    *,
    csv_options: CsvOptions | None,
    sheet: str | None,
    has_header: bool,
    ref_indexes: Sequence[int],
    transform: Callable[[str], str],
    progress_callback: ProgressCallback | None,
    cancel: threading.Event | None,
) -> int:
    connection.execute("CREATE TABLE ref_keys (key TEXT PRIMARY KEY) WITHOUT ROWID")
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    stream = open_stream(
        reference_path, csv_options=csv_options, sheet=sheet, has_header=has_header
    )
    rows = 0
    batch: list[tuple[str]] = []
    try:
        for row in stream.rows():
            if cancel is not None and cancel.is_set():
                break
            rows += 1
            batch.append((_row_key(row.cells, ref_indexes, transform),))
            if len(batch) >= _BATCH_SIZE:
                connection.executemany("INSERT OR IGNORE INTO ref_keys (key) VALUES (?)", batch)
                batch.clear()
            if progress_callback is not None and rows % 50_000 == 0:
                progress_callback("reference", rows, None)
    finally:
        stream.close()
    if batch:
        connection.executemany("INSERT OR IGNORE INTO ref_keys (key) VALUES (?)", batch)
    connection.commit()
    if progress_callback is not None:
        progress_callback("reference", rows, None)
    return rows


def extract_duplicates(
    *,
    scan_db_path: str | Path,
    rule_id: str,
    source_path: str | Path,
    source_csv_options: CsvOptions | None,
    source_header: list[str] | None,
    source_width: int,
    reference_path: str | Path,
    reference_csv_options: CsvOptions | None,
    reference_sheet: str | None,
    reference_has_header: bool,
    mapping: ColumnMapping,
    settings: Settings,
    id_column: int,
    out_path: str | Path,
    out_separator: str = ",",
    ref_db_path: str | Path | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> ExtractStats:
    """Write the duplicate rows of one rule to ``out_path`` (see module doc).

    ``scan_db_path`` is opened read-only, so this can safely run in a
    separate process while the GUI keeps browsing the report.
    ``ref_db_path`` lets the caller own the temporary reference-key database
    (the GUI passes a parent-owned path so it survives no child crash).
    """
    if not mapping.pairs:
        raise ValueError("the column mapping must align at least one column")
    transform = _value_transform(settings)
    source_indexes = mapping.source_indexes()

    if ref_db_path is None:
        ref_db_path = make_temp_store_path(prefix="lsa-refkeys-")
    ref_db_path = Path(ref_db_path)
    ref_db = sqlite3.connect(ref_db_path)
    store = ResultStore(scan_db_path, read_only=True)
    source = MatchRowSource(source_path, source_csv_options)
    reference_rows = 0
    groups_processed = 0
    kept_rows = 0
    case_a_rows = 0
    case_b_rows = 0
    cancelled = False

    def is_in_reference(key: str) -> bool:
        return ref_db.execute("SELECT 1 FROM ref_keys WHERE key = ?", (key,)).fetchone() is not None

    try:
        reference_rows = _ingest_reference(
            ref_db,
            reference_path,
            csv_options=reference_csv_options,
            sheet=reference_sheet,
            has_header=reference_has_header,
            ref_indexes=mapping.reference_indexes(),
            transform=transform,
            progress_callback=progress_callback,
            cancel=cancel,
        )
        if cancel is not None and cancel.is_set():
            return ExtractStats(reference_rows, 0, 0, 0, 0, cancelled=True)

        total_groups = store.group_count(rule_id)
        with open(out_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.writer(out_file, delimiter=out_separator)
            if source_header is not None:
                padded = list(source_header) + [""] * (source_width - len(source_header))
                writer.writerow(padded)

            def write_row(cells: Sequence[str]) -> None:
                padded = list(cells) + [""] * max(0, source_width - len(cells))
                writer.writerow(padded)

            current_group: object = object()
            current_sub: object = object()
            best: tuple[tuple, Sequence[str]] | None = None

            def flush_sub() -> None:
                # The surviving (lowest-ID) row of a finished sub-group is kept.
                nonlocal best, kept_rows
                if best is not None:
                    kept_rows += 1
                    best = None

            for match in store.iter_grouped_matches(rule_id):
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                cells = source.cells_for(match)
                if match.group_key != current_group:
                    flush_sub()
                    current_group = match.group_key
                    current_sub = object()
                    groups_processed += 1
                    if progress_callback is not None and groups_processed % 1000 == 0:
                        progress_callback("groups", groups_processed, total_groups)

                if match.sub_key is None:
                    # Case B: unique right-hand cells -> keep only when an
                    # identical row (over the mapped columns) exists in the
                    # reference file.
                    flush_sub()
                    if is_in_reference(_row_key(cells, source_indexes, transform)):
                        kept_rows += 1
                    else:
                        write_row(cells)
                        case_b_rows += 1
                else:
                    # Case A: identical right-hand cells -> lowest ID wins,
                    # all other rows go to the duplicate file.  Streamed:
                    # only the current best row is held in memory.
                    id_key = _id_sort_key(cells, id_column, match.row_number)
                    if match.sub_key != current_sub:
                        flush_sub()
                        current_sub = match.sub_key
                        best = (id_key, cells)
                    elif best is not None and id_key < best[0]:
                        write_row(best[1])
                        case_a_rows += 1
                        best = (id_key, cells)
                    else:
                        write_row(cells)
                        case_a_rows += 1
            flush_sub()
        if progress_callback is not None:
            progress_callback("groups", groups_processed, total_groups)
    finally:
        source.close()
        store.close()
        ref_db.close()
        ref_db_path.unlink(missing_ok=True)

    return ExtractStats(
        reference_rows=reference_rows,
        groups_processed=groups_processed,
        kept_rows=kept_rows,
        case_a_rows=case_a_rows,
        case_b_rows=case_b_rows,
        cancelled=cancelled,
    )
