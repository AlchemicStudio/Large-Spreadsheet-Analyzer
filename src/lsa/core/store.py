"""SQLite-backed storage of scan matches: counts, pagination, streaming export.

Matches are stored as ``(rule_id, row_number, byte_offset, cells)``.  For CSV
files with reliable offsets only the byte offset is kept (previews re-read the
source file); for workbook formats the matched row's cells are cached as JSON
so previews never re-open the workbook.  The database lives on disk, keeping
RAM usage flat no matter how many rows match.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    rule_id    TEXT    NOT NULL,
    row_number INTEGER NOT NULL,
    byte_offset INTEGER,
    cells      TEXT,
    PRIMARY KEY (rule_id, row_number)
) WITHOUT ROWID;
"""


class StoredMatch(NamedTuple):
    """One stored match; ``cells`` is only set when the row cache is used."""

    rule_id: str
    row_number: int
    byte_offset: int | None
    cells: list[str] | None


class ResultStore:
    """Result storage for one scan run.

    The store is written by the scan (possibly from a worker thread) and read
    afterwards for the report; access is sequential, never concurrent, which
    is why ``check_same_thread=False`` is safe here.
    """

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - we only want the name
                prefix="lsa-results-", suffix=".sqlite3", delete=False
            )
            handle.close()
            db_path = handle.name
            self._owns_file = True
        else:
            self._owns_file = False
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.execute("PRAGMA journal_mode=OFF")
        self._conn.execute("PRAGMA synchronous=OFF")

    def clear(self) -> None:
        """Remove all matches (e.g. before re-running a scan)."""
        self._conn.execute("DELETE FROM matches")
        self._conn.commit()

    def add_matches(self, batch: Iterable[tuple[str, int, int | None, str | None]]) -> None:
        """Insert ``(rule_id, row_number, byte_offset, cells_json)`` tuples."""
        self._conn.executemany(
            "INSERT OR REPLACE INTO matches (rule_id, row_number, byte_offset, cells) "
            "VALUES (?, ?, ?, ?)",
            batch,
        )

    def flush(self) -> None:
        """Commit pending inserts."""
        self._conn.commit()

    def count(self, rule_id: str) -> int:
        """Number of matches stored for one rule."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM matches WHERE rule_id = ?", (rule_id,)
        ).fetchone()
        return int(row[0])

    def counts(self) -> dict[str, int]:
        """Match count per rule id (only rules with at least one match appear)."""
        rows = self._conn.execute(
            "SELECT rule_id, COUNT(*) FROM matches GROUP BY rule_id"
        ).fetchall()
        return {rule_id: int(n) for rule_id, n in rows}

    @staticmethod
    def _to_match(row: tuple[str, int, int | None, str | None]) -> StoredMatch:
        rule_id, row_number, byte_offset, cells_json = row
        cells = None if cells_json is None else json.loads(cells_json)
        return StoredMatch(rule_id, row_number, byte_offset, cells)

    def get_page(self, rule_id: str, offset: int, limit: int) -> list[StoredMatch]:
        """Matches of one rule ordered by row number, paginated."""
        rows = self._conn.execute(
            "SELECT rule_id, row_number, byte_offset, cells FROM matches "
            "WHERE rule_id = ? ORDER BY row_number LIMIT ? OFFSET ?",
            (rule_id, limit, offset),
        ).fetchall()
        return [self._to_match(r) for r in rows]

    def iter_matches(self, rule_id: str) -> Iterator[StoredMatch]:
        """Stream all matches of one rule in row order (for exports)."""
        cursor = self._conn.execute(
            "SELECT rule_id, row_number, byte_offset, cells FROM matches "
            "WHERE rule_id = ? ORDER BY row_number",
            (rule_id,),
        )
        for row in cursor:
            yield self._to_match(row)

    def close(self) -> None:
        """Close the connection and delete the backing file if we created it."""
        self._conn.close()
        if self._owns_file:
            self.db_path.unlink(missing_ok=True)

    def __enter__(self) -> ResultStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
