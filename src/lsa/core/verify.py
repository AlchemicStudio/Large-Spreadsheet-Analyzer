"""Verify a generated duplicate file against the original scan and reference.

Every row of the duplicate file gets a **Row Key**: the rule's duplicate key
columns plus every cell to their right, under the same trim/case semantics
as the scan.  The row is a VALID duplicate when either:

- **Condition A** — the Row Key appears *multiple times* in the original
  input file (the matching original row indices and their first cells are
  listed), or
- **Condition B** — the Row Key appears *exactly once* in the original file
  AND is absent from the reference file (over the mapped columns).

Anything else (zero occurrences, or a unique row that *is* in the
reference) is flagged INVALID — the audit trail for the extraction.

Occurrence lookups ride the scan store's ``idx_dup_entries`` index and the
results land in their own on-disk SQLite, so verification streams at any
file size and the viewer paginates/searches without loading anything.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .csv_stream import CsvOptions, CsvRowStream
from .evaluate import compile_rules
from .extract import ColumnMapping, _row_key, _value_transform
from .report import MatchRowSource
from .rules import RuleSet, Settings
from .store import ResultStore

_BATCH_SIZE = 500
_MAX_MATCH_DISPLAY = 20

ProgressCallback = Callable[[int, int | None], None]
"""(rows verified, total rows or None)."""


@dataclass(frozen=True, slots=True)
class VerifyStats:
    """Outcome of one verification run.

    ``malformed_rows`` counts duplicate-file records the CSV reader could
    not parse — they received no verdict and deserve attention.
    """

    total: int
    valid_a: int
    valid_b: int
    invalid: int
    cancelled: bool = False
    malformed_rows: int = 0

    @property
    def valid(self) -> int:
        return self.valid_a + self.valid_b


class VerifRow(NamedTuple):
    """One verification result row."""

    seq: int
    first_cell: str
    second_cell: str
    valid: bool
    condition: str  # 'A', 'B' or ''
    occurrences: int
    matches_display: str
    cells: list[str]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS verif (
    seq        INTEGER PRIMARY KEY,
    first_cell TEXT    NOT NULL,
    second_cell TEXT   NOT NULL,
    valid      INTEGER NOT NULL,
    condition  TEXT    NOT NULL,
    occurrences INTEGER NOT NULL,
    matches_display TEXT NOT NULL,
    cells      TEXT    NOT NULL
) WITHOUT ROWID;
"""


class VerificationStore:
    """Writer (verification subprocess) and reader (viewer) for results."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        self.db_path = Path(db_path)
        if read_only:
            self._conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
            )
        else:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=OFF")
            self._conn.execute("PRAGMA synchronous=OFF")

    def add_batch(self, batch: list[tuple[int, str, str, int, str, int, str, str]]) -> None:
        self._conn.executemany(
            "INSERT OR REPLACE INTO verif "
            "(seq, first_cell, second_cell, valid, condition, occurrences, "
            " matches_display, cells) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )

    def flush(self) -> None:
        self._conn.commit()

    @staticmethod
    def _filter_clause(filter_text: str) -> tuple[str, list[str]]:
        if not filter_text:
            return "", []
        escaped = filter_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        return (
            " WHERE (first_cell LIKE ? ESCAPE '\\' OR second_cell LIKE ? ESCAPE '\\')",
            [like, like],
        )

    def count(self, filter_text: str = "") -> int:
        clause, params = self._filter_clause(filter_text)
        row = self._conn.execute(f"SELECT COUNT(*) FROM verif{clause}", params).fetchone()
        return int(row[0])

    def page(self, offset: int, limit: int, filter_text: str = "") -> list[VerifRow]:
        clause, params = self._filter_clause(filter_text)
        rows = self._conn.execute(
            "SELECT seq, first_cell, second_cell, valid, condition, occurrences, "
            f"matches_display, cells FROM verif{clause} ORDER BY seq LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [
            VerifRow(seq, fc, sc, bool(valid), cond, occ, disp, json.loads(cells))
            for seq, fc, sc, valid, cond, occ, disp, cells in rows
        ]

    def position_of(self, seq: int, filter_text: str = "") -> int:
        """0-based position of ``seq`` within the (filtered) result order."""
        clause, params = self._filter_clause(filter_text)
        glue = " AND" if clause else " WHERE"
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM verif{clause}{glue} seq < ?", [*params, seq]
        ).fetchone()
        return int(row[0])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> VerificationStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def verify_duplicates(
    *,
    scan_db_path: str | Path,
    ruleset: RuleSet,
    rule_id: str,
    source_path: str | Path,
    source_csv_options: CsvOptions | None,
    source_header: list[str] | None,
    source_width: int,
    dup_file_path: str | Path,
    dup_separator: str,
    mapping: ColumnMapping,
    settings: Settings,
    ref_db_path: str | Path,
    out_db_path: str | Path,
    progress_callback: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> VerifyStats:
    """Verify every row of ``dup_file_path``; results go to ``out_db_path``."""
    ref_path = Path(ref_db_path)
    if not ref_path.is_file():
        raise ValueError("the reference data of the extraction is no longer available")

    compiled = compile_rules(ruleset, source_header, source_width)
    rule = next((r for r in compiled.rules if r.rule_id == rule_id), None)
    if rule is None or not rule.dup_conds:
        raise ValueError(f"rule {rule_id!r} has no duplicate condition to verify")
    cond = rule.dup_conds[0]

    transform = _value_transform(settings)
    source_indexes = mapping.source_indexes()

    scan_store = ResultStore(scan_db_path, read_only=True)
    ref_db = sqlite3.connect(f"file:{ref_path}?mode=ro", uri=True)
    source_rows = MatchRowSource(source_path, source_csv_options)
    out = VerificationStore(out_db_path)

    dup_options = CsvOptions(separator=dup_separator, has_header=source_header is not None)

    total = valid_a = valid_b = invalid = 0
    cancelled = False
    batch: list[tuple[int, str, str, int, str, int, str, str]] = []

    def in_reference(cells: list[str]) -> bool:
        key = _row_key(cells, source_indexes, transform)
        return ref_db.execute("SELECT 1 FROM ref_keys WHERE key = ?", (key,)).fetchone() is not None

    def matches_display(occurrences: list) -> str:
        parts = []
        for match in occurrences[:_MAX_MATCH_DISPLAY]:
            cells = source_rows.cells_for(match)
            first = cells[0] if cells else ""
            parts.append(f"{match.row_number}:{first}")
        text = ", ".join(parts)
        hidden = len(occurrences) - _MAX_MATCH_DISPLAY
        if hidden > 0:
            text += f" (+{hidden})"
        return text

    malformed = 0
    try:
        with CsvRowStream(dup_file_path, dup_options) as dup_stream:
            # keep_trailing_empty: the duplicate file is machine-written;
            # even fully-empty rows are real extracted records to audit.
            for row in dup_stream.rows(keep_trailing_empty=True):
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                cells = row.cells
                occurrences = scan_store.find_dup_occurrences(
                    cond.cond_id, cond.key_of(cells), cond.right_key_of(cells)
                )
                total += 1
                if len(occurrences) > 1:
                    valid, condition = True, "A"
                    valid_a += 1
                elif len(occurrences) == 1 and not in_reference(cells):
                    valid, condition = True, "B"
                    valid_b += 1
                else:
                    # zero occurrences, or unique-but-present-in-reference:
                    # this row should not be in the duplicate file.
                    valid, condition = False, ""
                    invalid += 1
                batch.append(
                    (
                        total,
                        cells[0] if len(cells) > 0 else "",
                        cells[1] if len(cells) > 1 else "",
                        int(valid),
                        condition,
                        len(occurrences),
                        matches_display(occurrences),
                        json.dumps(cells, ensure_ascii=False),
                    )
                )
                if len(batch) >= _BATCH_SIZE:
                    out.add_batch(batch)
                    batch.clear()
                if progress_callback is not None and total % 2000 == 0:
                    progress_callback(total, None)
            malformed = dup_stream.malformed_rows
        if batch:
            out.add_batch(batch)
        out.flush()
        if progress_callback is not None:
            progress_callback(total, total)
    finally:
        source_rows.close()
        scan_store.close()
        ref_db.close()
        out.close()

    return VerifyStats(
        total=total,
        valid_a=valid_a,
        valid_b=valid_b,
        invalid=invalid,
        cancelled=cancelled,
        malformed_rows=malformed,
    )
