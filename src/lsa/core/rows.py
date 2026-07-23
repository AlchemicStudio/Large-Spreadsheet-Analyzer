"""Row stream abstraction shared by all file formats."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple, Protocol


class Row(NamedTuple):
    """One data row of the scanned file.

    ``number`` is the 1-based *physical* row number in the source file (the
    header, when present, is row 1).  ``offset`` is the byte offset of the row
    start, only set for CSV files whose encoding allows reliable seeking.
    """

    number: int
    cells: list[str]
    offset: int | None


class RowStream(Protocol):
    """Streaming, forward-only access to the rows of a spreadsheet file.

    Implementations read the header (when present) eagerly on construction so
    that rules can be compiled before iteration starts, then yield data rows
    lazily via :meth:`rows`.
    """

    header: list[str] | None
    width: int
    total_rows: int | None
    supports_offsets: bool

    def rows(self) -> Iterator[Row]:
        """Yield the data rows (the header row is not included)."""
        ...

    def bytes_read(self) -> int | None:
        """Bytes consumed so far, when byte-based progress is available."""
        ...

    def total_bytes(self) -> int | None:
        """Total size in bytes, when byte-based progress is available."""
        ...

    def close(self) -> None:
        """Release the underlying file handles."""
        ...


class FileFormat(StrEnum):
    """Supported input formats."""

    CSV = "csv"
    XLS = "xls"
    XLSX = "xlsx"
    ODS = "ods"


class UnsupportedFormatError(ValueError):
    """The file extension does not match any supported spreadsheet format."""


_EXTENSIONS: dict[str, FileFormat] = {
    ".csv": FileFormat.CSV,
    ".xls": FileFormat.XLS,
    ".xlsx": FileFormat.XLSX,
    ".xlsm": FileFormat.XLSX,
    ".ods": FileFormat.ODS,
}


def detect_format(path: str | Path) -> FileFormat:
    """Determine the file format from the file extension."""
    suffix = Path(path).suffix.lower()
    try:
        return _EXTENSIONS[suffix]
    except KeyError:
        supported = ", ".join(sorted(_EXTENSIONS))
        raise UnsupportedFormatError(
            f"unsupported file type {suffix!r} (supported: {supported})"
        ) from None


def cell_to_text(value: object) -> str:
    """Convert a typed spreadsheet cell value to its text form.

    The engine compares cells as text; this mirrors what a spreadsheet
    displays: integral floats lose the trailing ``.0``, booleans become
    TRUE/FALSE, dates/times use ISO format (midnight-only datetimes are
    shown as plain dates, matching date-formatted cells).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, dt.datetime):
        if value.time() == dt.time.min:
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    return str(value)


def suppress_trailing_empty(rows_iter: Iterator[Row], *, max_buffer: int = 10_000) -> Iterator[Row]:
    """Drop the run of fully-empty rows at the end of a stream.

    Spreadsheet files frequently claim more rows than they visibly contain
    (stale XLSX dimensions, trailing newlines in CSV); without this, an
    ``is_empty`` rule would report matches on rows the user cannot see.
    Interior empty rows are preserved.  To bound memory, a run longer than
    ``max_buffer`` is flushed and treated as data.
    """
    buffer: list[Row] = []
    for row in rows_iter:
        if all(cell.strip() == "" for cell in row.cells):
            buffer.append(row)
            if len(buffer) >= max_buffer:
                yield from buffer
                buffer.clear()
        else:
            if buffer:
                yield from buffer
                buffer.clear()
            yield row
