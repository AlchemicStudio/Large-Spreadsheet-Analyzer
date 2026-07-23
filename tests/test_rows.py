"""Cell text conversion, format detection and trailing-empty suppression."""

import datetime as dt

import pytest

from lsa.core.rows import (
    FileFormat,
    Row,
    UnsupportedFormatError,
    cell_to_text,
    detect_format,
    suppress_trailing_empty,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("x", "x"),
        (" spaced ", " spaced "),
        (True, "TRUE"),
        (False, "FALSE"),
        (3, "3"),
        (3.0, "3"),
        (3.5, "3.5"),
        (-2.0, "-2"),
        (dt.date(2024, 1, 5), "2024-01-05"),
        (dt.datetime(2024, 1, 5, 0, 0), "2024-01-05"),
        (dt.datetime(2024, 1, 5, 10, 30), "2024-01-05 10:30:00"),
        (dt.time(10, 30), "10:30:00"),
        (dt.timedelta(hours=1, minutes=30), "1:30:00"),
    ],
)
def test_cell_to_text(value: object, expected: str) -> None:
    assert cell_to_text(value) == expected


@pytest.mark.parametrize(
    ("name", "fmt"),
    [
        ("a.csv", FileFormat.CSV),
        ("a.CSV", FileFormat.CSV),
        ("a.xls", FileFormat.XLS),
        ("a.xlsx", FileFormat.XLSX),
        ("a.xlsm", FileFormat.XLSX),
        ("a.ods", FileFormat.ODS),
    ],
)
def test_detect_format(name: str, fmt: FileFormat) -> None:
    assert detect_format(name) is fmt


def test_detect_format_rejects_unknown() -> None:
    with pytest.raises(UnsupportedFormatError, match="unsupported file type"):
        detect_format("a.parquet")


def _rows(*cells_lists: list[str]) -> list[Row]:
    return [Row(i + 1, cells, None) for i, cells in enumerate(cells_lists)]


def test_trailing_empty_rows_dropped_interior_kept() -> None:
    rows = _rows(["a"], [], ["  ", ""], ["b"], [""], ["", "  "])
    kept = list(suppress_trailing_empty(iter(rows)))
    assert [r.number for r in kept] == [1, 2, 3, 4]


def test_all_empty_stream_yields_nothing() -> None:
    assert list(suppress_trailing_empty(iter(_rows([], ["", ""])))) == []


def test_suppression_buffer_cap_flushes() -> None:
    rows = _rows(*([[""]] * 5))
    kept = list(suppress_trailing_empty(iter(rows), max_buffer=3))
    assert [r.number for r in kept] == [1, 2, 3]  # capped run flushed, remainder dropped
