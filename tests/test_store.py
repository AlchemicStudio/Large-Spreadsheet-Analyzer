"""Result store: inserts, counts, pagination, streaming iteration."""

import json
from pathlib import Path

from lsa.core.store import ResultStore, StoredMatch


def _fill(store: ResultStore) -> None:
    store.add_matches(
        [("r1", n, n * 10, None) for n in range(1, 11)]
        + [("r2", 5, None, json.dumps(["a", "b"]))]
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
