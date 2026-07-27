"""SQLite-backed storage of scan matches: counts, pagination, streaming export.

Matches are stored as ``(rule_id, row_number, byte_offset, cells)``.  For CSV
files with reliable offsets only the byte offset is kept (previews re-read the
source file); for workbook formats the matched row's cells are cached as JSON
so previews never re-open the workbook.  The database lives on disk, keeping
RAM usage flat no matter how many rows match.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    rule_id    TEXT    NOT NULL,
    row_number INTEGER NOT NULL,
    byte_offset INTEGER,
    cells      TEXT,
    group_key  TEXT,
    sub_rank   INTEGER,
    sub_key    TEXT,
    PRIMARY KEY (rule_id, row_number)
) WITHOUT ROWID;
-- Pagination sorts by (group_key, sub_rank, sub_key, row_number); without
-- this index every report page would re-sort the whole (possibly
-- multi-million row) result.
CREATE INDEX IF NOT EXISTS idx_matches_order
    ON matches (rule_id, group_key, sub_rank, sub_key, row_number);
CREATE TABLE IF NOT EXISTS dup_entries (
    cond_id    TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    right_key  TEXT    NOT NULL,
    row_number INTEGER NOT NULL,
    byte_offset INTEGER,
    cells      TEXT,
    candidate  INTEGER NOT NULL
);
"""


class StoredMatch(NamedTuple):
    """One stored match; ``cells`` is only set when the row cache is used.

    ``group_key`` is set for is_duplicate matches: rows sharing it form one
    duplicate group (pagination orders by it so groups stay together).
    Within a duplicate group, ``sub_key`` holds the content of the cells to
    the right of the key columns when at least two rows share it; it is
    None for rows whose right-hand cells are unique (sorted last in their
    group) and for per-row-condition matches.
    """

    rule_id: str
    row_number: int
    byte_offset: int | None
    cells: list[str] | None
    group_key: str | None = None
    sub_key: str | None = None


_TEMP_PREFIX = "lsa-results-"
_TEMP_SUFFIX = ".sqlite3"
_STALE_AFTER_SECONDS = 2 * 24 * 3600
_stale_cleanup_done = False


def _default_store_dir() -> Path | None:
    """Directory for temporary result stores.

    On most Linux systems the default temp dir (/tmp) is tmpfs, i.e. backed
    by RAM: a scan producing tens of millions of matches would silently
    balloon memory and can get the application OOM-killed on a large file.
    /var/tmp is disk-backed by convention, so prefer it there.  Windows and
    macOS temp dirs are disk-backed already (None = tempfile's default).
    """
    if sys.platform.startswith("linux"):
        var_tmp = Path("/var/tmp")
        if var_tmp.is_dir() and os.access(var_tmp, os.W_OK):
            return var_tmp
    return None


def _cleanup_stale_stores(directory: Path) -> None:
    """Best-effort removal of result stores leaked by killed/crashed runs."""
    global _stale_cleanup_done
    if _stale_cleanup_done:
        return
    _stale_cleanup_done = True
    cutoff = time.time() - _STALE_AFTER_SECONDS
    with contextlib.suppress(OSError):
        for leftover in directory.glob(f"{_TEMP_PREFIX}*{_TEMP_SUFFIX}"):
            with contextlib.suppress(OSError):
                if leftover.stat().st_mtime < cutoff:
                    leftover.unlink()


def make_temp_store_path() -> Path:
    """Create an empty temp file for a result store and return its path.

    Callers using this directly (instead of ``ResultStore()``) own the file
    and must delete it themselves — used by the GUI, where the scan process
    writes the store and the UI process reads and later removes it.
    """
    store_dir = _default_store_dir()
    _cleanup_stale_stores(store_dir if store_dir is not None else Path(tempfile.gettempdir()))
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - we only want the name
        prefix=_TEMP_PREFIX,
        suffix=_TEMP_SUFFIX,
        dir=None if store_dir is None else str(store_dir),
        delete=False,
    )
    handle.close()
    return Path(handle.name)


class ResultStore:
    """Result storage for one scan run.

    The store is written by the scan (possibly from a worker thread) and read
    afterwards for the report; access is sequential, never concurrent, which
    is why ``check_same_thread=False`` is safe here.
    """

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = make_temp_store_path()
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
        self._conn.execute("DELETE FROM dup_entries")
        self._conn.commit()

    def add_matches(self, batch: Iterable[tuple[str, int, int | None, str | None]]) -> None:
        """Insert ``(rule_id, row_number, byte_offset, cells_json)`` tuples."""
        self._conn.executemany(
            "INSERT OR REPLACE INTO matches (rule_id, row_number, byte_offset, cells) "
            "VALUES (?, ?, ?, ?)",
            batch,
        )

    def add_dup_entries(
        self, batch: Iterable[tuple[str, str, str, int, int | None, str | None, int]]
    ) -> None:
        """Insert ``(cond_id, key, right_key, row_number, byte_offset, cells_json, candidate)``.

        One entry per data row per is_duplicate condition; ``candidate``
        records whether the row passed the rule's per-row conditions
        (meaningful for AND rules only); ``right_key`` is the row content to
        the right of the key columns, used for report sub-grouping.
        """
        self._conn.executemany(
            "INSERT INTO dup_entries "
            "(cond_id, key, right_key, row_number, byte_offset, cells, candidate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            batch,
        )

    def resolve_duplicates(self, rule_id: str, cond_ids: list[str], match: str) -> None:
        """Turn duplicate groups into matches for one rule (after the pass).

        A key is "duplicated" when it appears in more than one row of the
        file.  ``match='all'``: rows must have passed the per-row conditions
        (candidate=1) and be duplicated for *every* condition.  ``match='any'``:
        any duplicated condition matches the row, joining the per-row matches
        already inserted during the pass.

        Within each duplicate group, matches are sub-grouped by the row
        content to the right of the key columns: rows sharing that content
        get ``sub_rank=0`` and ``sub_key`` set (one sub-group per identical
        content); rows whose right-hand content is unique in their group get
        ``sub_rank=1`` (sorted last).  With several is_duplicate conditions
        in one AND rule, the sub-grouping follows the first condition.
        """
        if not cond_ids:
            return
        # Bulk phase over potentially tens of millions of rows: a large page
        # cache pays for itself; the index makes the GROUP BYs straight
        # index scans; ORDER BY row_number keeps the matches PK appends
        # mostly sequential.
        self._conn.execute("PRAGMA cache_size = -131072")  # 128 MB
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dup_entries ON dup_entries (cond_id, key, right_key)"
        )
        duplicated = (
            "SELECT key FROM dup_entries WHERE cond_id = ? GROUP BY key HAVING COUNT(*) > 1"
        )

        def insert_sql(candidate_filter: str) -> str:
            right_counts = (
                "SELECT key, right_key, COUNT(*) AS n FROM dup_entries "
                f"WHERE cond_id = ?{candidate_filter} GROUP BY key, right_key"
            )
            return (
                "INSERT OR REPLACE INTO matches "
                "(rule_id, row_number, byte_offset, cells, group_key, sub_rank, sub_key) "
                "SELECT ?, d.row_number, d.byte_offset, d.cells, d.key, "
                "CASE WHEN rc.n > 1 THEN 0 ELSE 1 END, "
                "CASE WHEN rc.n > 1 THEN d.right_key ELSE NULL END "
                f"FROM dup_entries d JOIN ({right_counts}) rc "
                "ON rc.key = d.key AND rc.right_key = d.right_key "
                f"WHERE d.cond_id = ? AND d.key IN ({duplicated})"
            )

        if match == "any":
            for cond_id in cond_ids:
                self._conn.execute(
                    insert_sql("") + " ORDER BY d.row_number",
                    (rule_id, cond_id, cond_id, cond_id),
                )
        else:
            first, rest = cond_ids[0], cond_ids[1:]
            sql = insert_sql(" AND candidate = 1") + " AND d.candidate = 1"
            params: list[object] = [rule_id, first, first, first]
            for cond_id in rest:
                sql += (
                    " AND d.row_number IN (SELECT row_number FROM dup_entries "
                    f"WHERE cond_id = ? AND key IN ({duplicated}))"
                )
                params.extend([cond_id, cond_id])
            sql += " ORDER BY d.row_number"
            self._conn.execute(sql, params)
        self._conn.commit()

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
    def _to_match(
        row: tuple[str, int, int | None, str | None, str | None, str | None],
    ) -> StoredMatch:
        rule_id, row_number, byte_offset, cells_json, group_key, sub_key = row
        cells = None if cells_json is None else json.loads(cells_json)
        return StoredMatch(rule_id, row_number, byte_offset, cells, group_key, sub_key)

    # Duplicate groups must stay together in the report; inside a group the
    # identical right-hand sub-groups come first (sub_rank 0, by sub_key)
    # and unique-right rows last (sub_rank 1).  SQLite sorts NULL first, so
    # per-row-only matches precede the grouped ones; for plain rules every
    # grouping column is NULL and this is plain row order.  The order
    # matches idx_matches_order exactly, so pagination never sorts.
    _ORDER = "ORDER BY group_key, sub_rank, sub_key, row_number"

    def get_page(self, rule_id: str, offset: int, limit: int) -> list[StoredMatch]:
        """Matches of one rule, paginated (duplicate groups kept together)."""
        rows = self._conn.execute(
            "SELECT rule_id, row_number, byte_offset, cells, group_key, sub_key FROM matches "
            f"WHERE rule_id = ? {self._ORDER} LIMIT ? OFFSET ?",
            (rule_id, limit, offset),
        ).fetchall()
        return [self._to_match(r) for r in rows]

    def iter_matches(self, rule_id: str) -> Iterator[StoredMatch]:
        """Stream all matches of one rule (for exports), groups together."""
        cursor = self._conn.execute(
            "SELECT rule_id, row_number, byte_offset, cells, group_key, sub_key FROM matches "
            f"WHERE rule_id = ? {self._ORDER}",
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
