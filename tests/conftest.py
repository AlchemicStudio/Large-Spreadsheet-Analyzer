"""Shared fixture helpers."""

from pathlib import Path

import pytest


@pytest.fixture
def make_csv(tmp_path: Path):
    """Write a CSV file with the given text/bytes and return its path."""

    def _make(content: str | bytes, *, name: str = "data.csv", encoding: str = "utf-8") -> Path:
        path = tmp_path / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_bytes(content.encode(encoding))
        return path

    return _make


@pytest.fixture
def make_xlsx(tmp_path: Path):
    """Build an XLSX workbook from {sheet_name: rows} and return its path."""

    def _make(sheets: dict[str, list[list[object]]], *, name: str = "data.xlsx") -> Path:
        from openpyxl import Workbook

        wb = Workbook()
        default = wb.active
        for i, (sheet_name, rows) in enumerate(sheets.items()):
            ws = default if i == 0 else wb.create_sheet()
            ws.title = sheet_name
            for row in rows:
                ws.append(row)
        path = tmp_path / name
        wb.save(path)
        return path

    return _make
