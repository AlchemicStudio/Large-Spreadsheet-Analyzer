"""CSV streaming: dialects, encodings, byte offsets and seek-based re-reads."""

from dataclasses import replace
from pathlib import Path

import pytest

from lsa.core.csv_stream import (
    CsvOptions,
    CsvRowFetcher,
    CsvRowStream,
    detect_csv_options,
    encoding_supports_offsets,
)
from lsa.core.streams import open_stream


def test_basic_header_file(make_csv) -> None:
    path = make_csv("name,email\nAna,a@b.c\nBo,\n")
    with CsvRowStream(path, CsvOptions()) as stream:
        assert stream.header == ["name", "email"]
        assert stream.width == 2
        assert stream.supports_offsets is True
        rows = list(stream.rows())
    assert [(r.number, r.cells) for r in rows] == [(2, ["Ana", "a@b.c"]), (3, ["Bo", ""])]
    assert all(r.offset is not None for r in rows)


def test_headerless_numbers_from_one(make_csv) -> None:
    path = make_csv("Ana,a@b.c\nBo,b@c.d\n")
    with CsvRowStream(path, CsvOptions(has_header=False)) as stream:
        assert stream.header is None
        assert stream.width == 2
        numbers = [r.number for r in stream.rows()]
    assert numbers == [1, 2]


def test_semicolon_dialect(make_csv) -> None:
    path = make_csv('a;b\n"x;y";2\n')
    with CsvRowStream(path, CsvOptions(separator=";")) as stream:
        assert next(iter(stream.rows())).cells == ["x;y", "2"]


def test_offsets_seek_back_to_same_row(make_csv) -> None:
    path = make_csv('name,note\nAna,"line1\nline2"\nBo,plain\nCé,"x,y"\n')
    options = CsvOptions()
    with CsvRowStream(path, options) as stream:
        rows = list(stream.rows())
    assert [r.number for r in rows] == [2, 3, 4]
    assert rows[0].cells == ["Ana", "line1\nline2"]
    with CsvRowFetcher(path, options) as fetcher:
        for row in rows:
            assert fetcher.fetch(row.offset) == row.cells


def test_bom_stripped_and_offsets_still_work(make_csv) -> None:
    path = make_csv(b"\xef\xbb\xbfname,email\nAna,a@b.c\n")
    options = replace(detect_csv_options(path), has_header=True)
    assert options.encoding == "utf-8-sig"
    with CsvRowStream(path, options) as stream:
        assert stream.header == ["name", "email"]
        rows = list(stream.rows())
    assert rows[0].cells == ["Ana", "a@b.c"]


def test_detects_semicolon_and_utf8(make_csv) -> None:
    text = "nom;ville\nRené;Liège\nZoé;Namur\n" * 30
    path = make_csv(text, encoding="utf-8")
    options = replace(detect_csv_options(path), has_header=True)
    assert options.separator == ";"
    assert options.encoding == "utf-8"
    with CsvRowStream(path, options) as stream:
        assert next(iter(stream.rows())).cells == ["René", "Liège"]


def test_user_encoding_override_wins(make_csv) -> None:
    # Single-byte encodings are ambiguous to detect (cp1250 vs cp1252...);
    # the user-supplied encoding must be honored as-is.
    path = make_csv("nom;ville\nRené;Liège\n", encoding="cp1252")
    options = CsvOptions(separator=";", encoding="cp1252")
    with CsvRowStream(path, options) as stream:
        assert next(iter(stream.rows())).cells == ["René", "Liège"]


def test_utf16_falls_back_to_text_mode(make_csv) -> None:
    path = make_csv("name,email\nAna,a@b.c\nBo,b@c.d\n", encoding="utf-16")
    options = replace(detect_csv_options(path), has_header=True)
    assert not encoding_supports_offsets(options.encoding)
    with CsvRowStream(path, options) as stream:
        assert stream.supports_offsets is False
        rows = list(stream.rows())
    assert [r.cells for r in rows] == [["Ana", "a@b.c"], ["Bo", "b@c.d"]]
    assert all(r.offset is None for r in rows)


def test_cr_only_line_endings_fall_back(make_csv) -> None:
    path = make_csv("name,email\rAna,a@b.c\rBo,b@c.d\r")
    with CsvRowStream(path, CsvOptions()) as stream:
        assert stream.supports_offsets is False
        assert stream.header == ["name", "email"]
        assert [r.cells for r in stream.rows()] == [["Ana", "a@b.c"], ["Bo", "b@c.d"]]


def test_empty_file(make_csv) -> None:
    path = make_csv("")
    with CsvRowStream(path, CsvOptions()) as stream:
        assert stream.header is None
        assert stream.width == 0
        assert list(stream.rows()) == []


def test_blank_interior_row_kept_trailing_dropped(make_csv) -> None:
    path = make_csv("a,b\n1,2\n\n3,4\n\n\n")
    with CsvRowStream(path, CsvOptions()) as stream:
        rows = list(stream.rows())
    assert [(r.number, r.cells) for r in rows] == [(2, ["1", "2"]), (3, []), (4, ["3", "4"])]


def test_ragged_rows_preserved(make_csv) -> None:
    path = make_csv("a,b,c\n1\n1,2,3,4\n")
    with CsvRowStream(path, CsvOptions()) as stream:
        assert [r.cells for r in stream.rows()] == [["1"], ["1", "2", "3", "4"]]


def test_bytes_progress(make_csv) -> None:
    path = make_csv("a,b\n" + "x,y\n" * 100)
    with CsvRowStream(path, CsvOptions()) as stream:
        assert stream.total_bytes() == Path(path).stat().st_size
        for _ in stream.rows():
            pass
        assert stream.bytes_read() == stream.total_bytes()


def test_separator_must_be_single_char(make_csv) -> None:
    path = make_csv("a,b\n")
    with pytest.raises(ValueError, match="single character"):
        CsvRowStream(path, CsvOptions(separator="||"))


def test_open_stream_dispatches_csv(make_csv) -> None:
    path = make_csv("a,b\n1,2\n")
    stream = open_stream(path)
    try:
        assert isinstance(stream, CsvRowStream)
        assert stream.header == ["a", "b"]
    finally:
        stream.close()


def test_open_stream_header_override(make_csv) -> None:
    path = make_csv("a,b\n1,2\n" * 20)
    stream = open_stream(path, has_header=False)
    try:
        assert stream.header is None
        assert next(iter(stream.rows())).number == 1
    finally:
        stream.close()
