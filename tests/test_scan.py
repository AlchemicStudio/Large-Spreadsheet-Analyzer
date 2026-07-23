"""Scan engine: single pass, counts, progress, cancellation, cell caching."""

import threading

import pytest

from lsa.core.columns import ColumnRef, ColumnResolutionError
from lsa.core.csv_stream import CsvOptions, CsvRowFetcher, CsvRowStream
from lsa.core.excel_stream import OpenpyxlRowStream
from lsa.core.rules import Condition, Rule, RuleSet
from lsa.core.scan import ScanProgress, run_scan
from lsa.core.store import ResultStore

RULESET = RuleSet(
    rules=(
        Rule(
            id="no-email",
            label="Missing e-mail",
            match="all",
            conditions=(
                Condition("not_empty", ColumnRef("header", "name")),
                Condition("is_empty", ColumnRef("header", "email")),
            ),
        ),
        Rule(
            id="same-cols",
            label="name equals email",
            match="any",
            conditions=(
                Condition("equals_column", ColumnRef("letter", "A"), ColumnRef("letter", "B")),
            ),
        ),
    )
)

CSV_TEXT = "name,email\nAna,\nBo,b@c.d\nx,x\n,\nDee,\n"
# rows: 2 no-email; 3 clean; 4 same-cols; 5 empty name+email -> same-cols; 6 no-email


def test_scan_csv_counts_offsets_and_summary(make_csv, tmp_path) -> None:
    path = make_csv(CSV_TEXT)
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        summary = run_scan(stream, RULESET, store)
        assert summary.counts == {"no-email": 2, "same-cols": 2}
        assert summary.rows_scanned == 5
        assert summary.cancelled is False
        matches = store.get_page("no-email", 0, 10)
        assert [m.row_number for m in matches] == [2, 6]
        # offsets stored, cells not cached for offset-capable CSV
        assert all(m.byte_offset is not None and m.cells is None for m in matches)
        with CsvRowFetcher(path, CsvOptions()) as fetcher:
            assert fetcher.fetch(matches[0].byte_offset) == ["Ana", ""]


def test_scan_xlsx_caches_cells(make_xlsx, tmp_path) -> None:
    path = make_xlsx({"Data": [["name", "email"], ["Ana", None], ["x", "x"]]})
    stream = OpenpyxlRowStream(path)
    with ResultStore(tmp_path / "r.sqlite3") as store:
        try:
            summary = run_scan(stream, RULESET, store)
        finally:
            stream.close()
        assert summary.counts == {"no-email": 1, "same-cols": 1}
        match = store.get_page("no-email", 0, 10)[0]
        assert match.row_number == 2
        assert match.byte_offset is None
        assert match.cells == ["Ana", ""]


def test_progress_callback_and_final_snapshot(make_csv, tmp_path) -> None:
    path = make_csv("name,email\n" + "Ana,\n" * 50)
    snapshots: list[ScanProgress] = []
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        run_scan(
            stream,
            RULESET,
            store,
            progress_callback=snapshots.append,
            progress_interval_rows=10,
        )
    assert [s.rows_processed for s in snapshots] == [10, 20, 30, 40, 50, 50]
    final = snapshots[-1]
    assert final.counts["no-email"] == 50
    assert final.fraction == 1.0
    assert all(s.fraction is not None for s in snapshots)


def test_cancel_keeps_partial_results(make_csv, tmp_path) -> None:
    path = make_csv("name,email\n" + "Ana,\n" * 100)
    cancel = threading.Event()

    def stop_after_first_progress(_: ScanProgress) -> None:
        cancel.set()

    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        summary = run_scan(
            stream,
            RULESET,
            store,
            progress_callback=stop_after_first_progress,
            progress_interval_rows=10,
            cancel=cancel,
        )
        assert summary.cancelled is True
        assert summary.rows_scanned == 10
        assert store.count("no-email") == 10


def test_empty_file_scans_to_zero(make_csv, tmp_path) -> None:
    path = make_csv("")
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        summary = run_scan(stream, RULESET, store)
    assert summary.rows_scanned == 0
    assert summary.counts == {"no-email": 0, "same-cols": 0}
    assert summary.cancelled is False


def test_unresolvable_column_raises_before_scanning(make_csv, tmp_path) -> None:
    path = make_csv("a,b\n1,2\n")
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        with pytest.raises(ColumnResolutionError, match="no such header"):
            run_scan(stream, RULESET, store)
        assert store.counts() == {}
