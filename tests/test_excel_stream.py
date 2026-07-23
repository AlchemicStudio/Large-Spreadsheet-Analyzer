"""Workbook streams: openpyxl (XLSX) and calamine (XLS/ODS; exercised via XLSX)."""

import datetime as dt

import pytest

from lsa.core.excel_stream import (
    CalamineRowStream,
    OpenpyxlRowStream,
    SheetNotFoundError,
    list_sheets,
    needs_memory_warning,
)
from lsa.core.streams import open_stream

TYPED_ROWS = [
    ["name", "num", "flag", "when"],
    ["Ana", 3, True, dt.date(2024, 1, 5)],
    ["Bo", 3.5, False, dt.datetime(2024, 1, 5, 10, 30)],
    ["Cy", None, None, None],
]

EXPECTED_TEXT = [
    ["Ana", "3", "TRUE", "2024-01-05"],
    ["Bo", "3.5", "FALSE", "2024-01-05 10:30:00"],
    ["Cy", "", "", ""],
]


def _padded(cells: list[str], width: int) -> list[str]:
    """openpyxl read-only yields ragged rows once dimensions are recalculated."""
    return list(cells) + [""] * (width - len(cells))


@pytest.mark.parametrize("stream_cls", [OpenpyxlRowStream, CalamineRowStream])
def test_typed_cells_become_text(make_xlsx, stream_cls) -> None:
    path = make_xlsx({"Data": TYPED_ROWS})
    stream = stream_cls(path)
    try:
        assert stream.header == ["name", "num", "flag", "when"]
        assert stream.width == 4
        assert stream.supports_offsets is False
        rows = list(stream.rows())
    finally:
        stream.close()
    assert [r.number for r in rows] == [2, 3, 4]
    assert [_padded(r.cells, 4) for r in rows] == EXPECTED_TEXT
    assert all(r.offset is None for r in rows)


@pytest.mark.parametrize("stream_cls", [OpenpyxlRowStream, CalamineRowStream])
def test_headerless_numbering(make_xlsx, stream_cls) -> None:
    path = make_xlsx({"Data": [["a", 1], ["b", 2]]})
    stream = stream_cls(path, has_header=False)
    try:
        assert stream.header is None
        assert [(r.number, r.cells) for r in stream.rows()] == [
            (1, ["a", "1"]),
            (2, ["b", "2"]),
        ]
    finally:
        stream.close()


@pytest.mark.parametrize("stream_cls", [OpenpyxlRowStream, CalamineRowStream])
def test_offset_data_keeps_absolute_positions(make_xlsx, tmp_path, stream_cls) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    wb.active["C3"] = "x"
    wb.active["D4"] = "y"
    path = tmp_path / "offset.xlsx"
    wb.save(path)

    stream = stream_cls(path, has_header=False)
    try:
        rows = list(stream.rows())
    finally:
        stream.close()
    by_number = {r.number: r.cells for r in rows}
    assert by_number[3][2] == "x"
    assert by_number[4][3] == "y"


@pytest.mark.parametrize("stream_cls", [OpenpyxlRowStream, CalamineRowStream])
def test_sheet_selection(make_xlsx, stream_cls) -> None:
    path = make_xlsx({"First": [["a"]], "Second": [["b"]]})
    stream = stream_cls(path, sheet="Second", has_header=False)
    try:
        assert stream.sheet_name == "Second"
        assert [r.cells for r in stream.rows()] == [["b"]]
    finally:
        stream.close()
    default = stream_cls(path, has_header=False)
    try:
        assert default.sheet_name == "First"
    finally:
        default.close()


@pytest.mark.parametrize("stream_cls", [OpenpyxlRowStream, CalamineRowStream])
def test_unknown_sheet_lists_available(make_xlsx, stream_cls) -> None:
    path = make_xlsx({"Only": [["a"]]})
    with pytest.raises(SheetNotFoundError, match=r"no sheet named 'Nope'.*'Only'"):
        stream_cls(path, sheet="Nope")


def test_list_sheets(make_xlsx) -> None:
    path = make_xlsx({"Data": [["a"]], "Extra": [["b"]]})
    assert list_sheets(path) == ["Data", "Extra"]


def test_trailing_empty_rows_suppressed(make_xlsx) -> None:
    path = make_xlsx({"Data": [["h"], ["x"], [""], [None], [""]]})
    stream = OpenpyxlRowStream(path)
    try:
        assert [r.number for r in stream.rows()] == [2]
    finally:
        stream.close()


def test_total_rows_estimate_excludes_header(make_xlsx) -> None:
    path = make_xlsx({"Data": [["h"], ["a"], ["b"]]})
    for stream_cls in (OpenpyxlRowStream, CalamineRowStream):
        stream = stream_cls(path)
        try:
            assert stream.total_rows == 2  # data rows only, for progress math
        finally:
            stream.close()
        headerless = stream_cls(path, has_header=False)
        try:
            assert headerless.total_rows == 3
        finally:
            headerless.close()


@pytest.mark.parametrize("stream_cls", [OpenpyxlRowStream, CalamineRowStream])
def test_empty_sheet_yields_nothing(make_xlsx, stream_cls) -> None:
    # calamine 0.8 panics (PanicException!) if iter_rows() is called on a
    # sheet without a used range - the stream must never do that.
    path = make_xlsx({"Empty": []})
    stream = stream_cls(path)
    try:
        assert stream.width == 0
        assert stream.header is None
        assert list(stream.rows()) == []
    finally:
        stream.close()


def test_lying_dimension_does_not_truncate(make_xlsx, tmp_path) -> None:
    # Streaming XLSX writers often emit <dimension ref="A1"/> regardless of
    # content; in read-only mode openpyxl would bound iteration by it and
    # silently scan nothing.
    import re
    import zipfile

    src = make_xlsx({"Data": [["name", "email"], ["Ana", "a"], ["Bo", "b"]]})
    lied = tmp_path / "lied.xlsx"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(lied, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                patched = re.sub(rb"<dimension ref=\"[^\"]*\"", b'<dimension ref="A1"', data)
                assert patched != data, "fixture did not contain a dimension element"
                data = patched
            zout.writestr(item, data)

    stream = OpenpyxlRowStream(lied)
    try:
        assert stream.header == ["name", "email"]
        assert [_padded(r.cells, 2) for r in stream.rows()] == [["Ana", "a"], ["Bo", "b"]]
    finally:
        stream.close()


def test_open_stream_dispatches_xlsx(make_xlsx) -> None:
    path = make_xlsx({"Data": [["h"], ["a"]]})
    stream = open_stream(path)
    try:
        assert isinstance(stream, OpenpyxlRowStream)
    finally:
        stream.close()


def test_memory_warning_only_for_large_xls_ods(make_xlsx, tmp_path) -> None:
    big_ods = tmp_path / "big.ods"
    big_ods.write_bytes(b"0" * 1024)
    assert needs_memory_warning(big_ods, threshold_bytes=1024) is True
    assert needs_memory_warning(big_ods, threshold_bytes=2048) is False
    xlsx = make_xlsx({"Data": [["a"]]})
    assert needs_memory_warning(xlsx, threshold_bytes=0) is False
