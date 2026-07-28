"""Result store: inserts, counts, pagination, streaming iteration."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from lsa.core.store import ResultStore, StoredMatch


def _fill(store: ResultStore) -> None:
    store.add_matches(
        [("r1", n, n * 10, None) for n in range(1, 11)] + [("r2", 5, None, json.dumps(["a", "b"]))]
    )
    store.flush()


def test_counts_and_missing_rule(tmp_path: Path) -> None:
    with ResultStore(tmp_path / "res.sqlite3") as store:
        _fill(store)
        assert store.count("r1") == 10
        assert store.count("r2") == 1
        assert store.count("ghost") == 0
        assert store.counts() == {"r1": 10, "r2": 1}


def test_pagination_ordered_by_row_number(tmp_path: Path) -> None:
    with ResultStore(tmp_path / "res.sqlite3") as store:
        store.add_matches([("r1", n, None, None) for n in (7, 3, 9, 1, 5)])
        store.flush()
        page = store.get_page("r1", 0, 2)
        assert [m.row_number for m in page] == [1, 3]
        assert [m.row_number for m in store.get_page("r1", 2, 2)] == [5, 7]
        assert [m.row_number for m in store.get_page("r1", 4, 2)] == [9]
        assert store.get_page("r1", 6, 2) == []


def test_cells_json_round_trip(tmp_path: Path) -> None:
    with ResultStore(tmp_path / "res.sqlite3") as store:
        _fill(store)
        match = store.get_page("r2", 0, 10)[0]
        assert match == StoredMatch("r2", 5, None, ["a", "b"])
        offset_match = store.get_page("r1", 0, 1)[0]
        assert offset_match.byte_offset == 10
        assert offset_match.cells is None


def test_iter_matches_streams_in_order(tmp_path: Path) -> None:
    with ResultStore(tmp_path / "res.sqlite3") as store:
        _fill(store)
        assert [m.row_number for m in store.iter_matches("r1")] == list(range(1, 11))


def test_clear(tmp_path: Path) -> None:
    with ResultStore(tmp_path / "res.sqlite3") as store:
        _fill(store)
        store.clear()
        assert store.counts() == {}


def test_temp_file_store_cleans_up_after_close() -> None:
    store = ResultStore()
    db_path = store.db_path
    assert db_path.exists()
    store.close()
    assert not db_path.exists()


def test_temp_store_avoids_tmpfs_on_linux() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux-specific: /tmp is commonly tmpfs there")
    store = ResultStore()
    try:
        # /var/tmp is disk-backed; /tmp would put millions of matches in RAM.
        assert str(store.db_path.parent) == "/var/tmp"
    finally:
        store.close()


def test_stale_leaked_stores_are_removed(tmp_path: Path, monkeypatch) -> None:
    import lsa.core.store as store_module

    monkeypatch.setattr(store_module, "_default_store_dir", lambda: tmp_path)
    monkeypatch.setattr(store_module, "_stale_cleanup_done", False)
    stale_time = (time.time() - 3 * 24 * 3600, time.time() - 3 * 24 * 3600)
    old_files = []
    # every temp family the app creates must be swept, not just lsa-results
    for name in ("lsa-results-old.sqlite3", "lsa-refkeys-old.sqlite3", "lsa-duplicates-old.csv"):
        stale = tmp_path / name
        stale.write_bytes(b"x")
        os.utime(stale, stale_time)
        old_files.append(stale)
    fresh = tmp_path / "lsa-results-fresh.sqlite3"
    fresh.write_bytes(b"x")
    unrelated = tmp_path / "keep.sqlite3"
    unrelated.write_bytes(b"x")

    store = ResultStore()
    try:
        for stale in old_files:
            assert not stale.exists(), stale.name
        assert fresh.exists()
        assert unrelated.exists()
        assert store.db_path.parent == tmp_path
    finally:
        store.close()
