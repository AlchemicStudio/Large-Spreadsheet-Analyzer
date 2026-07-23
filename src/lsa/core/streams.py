"""Open the right RowStream implementation for a file."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .csv_stream import CsvOptions, CsvRowStream, detect_csv_options
from .excel_stream import CalamineRowStream, OpenpyxlRowStream
from .rows import FileFormat, RowStream, detect_format


def open_stream(
    path: str | Path,
    *,
    csv_options: CsvOptions | None = None,
    sheet: str | None = None,
    has_header: bool | None = None,
) -> RowStream:
    """Open ``path`` with the reader matching its format.

    ``csv_options`` applies to CSV files (auto-detected when omitted);
    ``sheet`` applies to workbook formats.  ``has_header`` overrides the
    detected/default header flag when not ``None``.
    """
    fmt = detect_format(path)
    if fmt is FileFormat.CSV:
        options = csv_options if csv_options is not None else detect_csv_options(path)
        if has_header is not None and options.has_header != has_header:
            options = replace(options, has_header=has_header)
        return CsvRowStream(path, options)
    header_flag = True if has_header is None else has_header
    if fmt is FileFormat.XLSX:
        return OpenpyxlRowStream(path, sheet=sheet, has_header=header_flag)
    return CalamineRowStream(path, sheet=sheet, has_header=header_flag)
