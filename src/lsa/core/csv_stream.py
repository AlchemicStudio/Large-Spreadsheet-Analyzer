"""Streaming CSV reader with byte-offset tracking, plus dialect/encoding detection.

The whole file is never loaded into memory: rows are parsed from a binary
file object line by line.  For ASCII-compatible encodings the byte offset of
every row start is recorded, so report previews can later ``seek()`` straight
to a matched row instead of caching it.
"""

from __future__ import annotations

import codecs
import contextlib
import csv
import io
import os
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

from charset_normalizer import from_bytes

from .rows import Row, suppress_trailing_empty

# Cap a single field at 1 MB.  Far larger than any real spreadsheet cell,
# small enough that a malformed file cannot balloon memory: a stray opening
# quote would otherwise make csv.reader swallow the whole remainder of a
# multi-GB file into ONE in-memory field and get the process OOM-killed.
# Records that overflow are skipped and counted (CsvRowStream.malformed_rows),
# and the cap also bounds how much data a runaway quote can swallow.
_MAX_FIELD_BYTES = 1024 * 1024
csv.field_size_limit(_MAX_FIELD_BYTES)

_SNIFF_DELIMITERS = ",;\t|"
_DETECT_SAMPLE_BYTES = 1 << 20
_PEEK_BYTES = 1 << 16


@dataclass(frozen=True, slots=True)
class CsvOptions:
    """CSV import options (the "dialect" collected from the user)."""

    separator: str = ","
    quotechar: str = '"'
    encoding: str = "utf-8"
    has_header: bool = True


def detect_csv_options(path: str | Path, *, sample_bytes: int = _DETECT_SAMPLE_BYTES) -> CsvOptions:
    """Pre-fill CSV options from the file: encoding via charset-normalizer,
    separator/quote via csv.Sniffer, header via Sniffer's heuristic.

    Detection only reads the first ``sample_bytes`` bytes; every value can be
    overridden by the user afterwards.
    """
    with open(path, "rb") as f:
        head = f.read(sample_bytes)

    encoding = "utf-8"
    if head:
        best = from_bytes(head).best()
        if best is not None:
            encoding = codecs.lookup(best.encoding).name
        if head.startswith(codecs.BOM_UTF8):
            encoding = "utf-8-sig"
        elif encoding == "utf-16" and not head.startswith(
            (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
        ):
            # Bare "utf-16" decodes as little-endian when no BOM is present,
            # which turns BOM-less big-endian files into silent garbage.
            # ASCII-heavy UTF-16 text has NUL high bytes: LE puts them at odd
            # offsets, BE at even offsets.
            even_nuls = head[0::2].count(0)
            odd_nuls = head[1::2].count(0)
            encoding = "utf-16-be" if even_nuls > odd_nuls else "utf-16-le"

    sample = head.decode(encoding, errors="replace")
    separator, quotechar, has_header = ",", '"', True
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(sample, delimiters=_SNIFF_DELIMITERS)
        separator = dialect.delimiter
        quotechar = dialect.quotechar or '"'
    except csv.Error:
        pass
    with contextlib.suppress(csv.Error):
        has_header = sniffer.has_header(sample)
    return CsvOptions(
        separator=separator, quotechar=quotechar, encoding=encoding, has_header=has_header
    )


def encoding_supports_offsets(encoding: str) -> bool:
    """Whether lines of this encoding can be split on the ``\\n`` byte.

    UTF-16/UTF-32 encode the newline in multiple bytes, so per-row byte
    offsets (and ``seek()``-based previews) are not available for them.
    """
    try:
        name = codecs.lookup(encoding).name
    except LookupError:
        return False
    return not name.startswith(("utf-16", "utf-32"))


class _OffsetLineIterator:
    """Yield decoded lines from a binary file, tracking byte offsets.

    ``next_offset`` is always the byte offset of the next line that will be
    yielded — sampled by the caller *before* pulling a CSV record, it is the
    offset of that record's first byte.

    A bare ``\\r`` inside a physical line terminates a logical line, exactly
    as ``open(..., newline="")`` would split it.  Without this, one stray CR
    (a mangled CRLF, text pasted from an old Mac app) would make csv.reader
    raise ``_csv.Error`` and abort the whole scan, while the text-mode
    fallback path would happily parse the same bytes.
    """

    __slots__ = ("_encoding", "_file", "_segments", "next_offset")

    def __init__(self, file: BinaryIO, encoding: str):
        self._file = file
        self._encoding = encoding
        self._segments: deque[bytes] = deque()
        self.next_offset = file.tell()

    def __iter__(self) -> _OffsetLineIterator:
        return self

    def __next__(self) -> str:
        if not self._segments:
            line = self._file.readline()
            if not line:
                raise StopIteration
            self._split_on_bare_cr(line)
        segment = self._segments.popleft()
        self.next_offset += len(segment)
        return segment.decode(self._encoding, errors="replace")

    def _split_on_bare_cr(self, line: bytes) -> None:
        while line:
            index = line.find(b"\r")
            if index == -1 or line[index : index + 2] == b"\r\n":
                self._segments.append(line)
                return
            self._segments.append(line[: index + 1])
            line = line[index + 1 :]


def _make_reader(source: Iterator[str], options: CsvOptions) -> Iterator[list[str]]:
    if len(options.separator) != 1:
        raise ValueError(f"separator must be a single character, got {options.separator!r}")
    if options.quotechar:
        return csv.reader(source, delimiter=options.separator, quotechar=options.quotechar)
    return csv.reader(source, delimiter=options.separator, quoting=csv.QUOTE_NONE)


class CsvRowStream:
    """A :class:`~lsa.core.rows.RowStream` over a CSV file."""

    def __init__(self, path: str | Path, options: CsvOptions | None = None):
        self.path = Path(path)
        self.options = options if options is not None else detect_csv_options(path)
        self._raw: BinaryIO = open(self.path, "rb")  # noqa: SIM115 - stream owns the handle
        try:
            self._size = os.fstat(self._raw.fileno()).st_size
            peek = self._raw.read(_PEEK_BYTES)
            self._raw.seek(0)
            # Classic-Mac files using bare \r line endings cannot be split on \n.
            cr_only = b"\n" not in peek and b"\r" in peek
            self.supports_offsets = encoding_supports_offsets(self.options.encoding) and not cr_only

            self._lines: _OffsetLineIterator | None = None
            self._text: io.TextIOWrapper | None = None
            if self.supports_offsets:
                self._lines = _OffsetLineIterator(self._raw, self.options.encoding)
                self._reader = _make_reader(self._lines, self.options)
            else:
                self._text = io.TextIOWrapper(
                    self._raw, encoding=self.options.encoding, errors="replace", newline=""
                )
                self._reader = _make_reader(self._text, self.options)

            self.header: list[str] | None = None
            self._pending: Row | None = None
            self.malformed_rows = 0
            first_offset = self._lines.next_offset if self._lines is not None else None
            try:
                first = next(self._reader, None)
            except csv.Error as exc:
                raise ValueError(
                    f"cannot parse the first row with these CSV settings: {exc}"
                ) from exc
            if first is None:
                self.width = 0
            elif self.options.has_header:
                self.header = first
                self.width = len(first)
            else:
                self.width = len(first)
                self._pending = Row(1, first, first_offset)
        except BaseException:
            self._raw.close()
            raise

    @property
    def total_rows(self) -> int | None:
        """Unknown for CSV (progress uses bytes instead)."""
        return None

    def _iter_raw(self) -> Iterator[Row]:
        number = 2 if self.options.has_header else 1
        if self._pending is not None:
            pending, self._pending = self._pending, None
            yield pending
            number = pending.number + 1
        while True:
            offset = self._lines.next_offset if self._lines is not None else None
            try:
                cells = next(self._reader)
            except StopIteration:
                return
            except csv.Error:
                # Malformed record (usually a runaway quoted field hitting the
                # field size cap): skip it and resume from the next line.  For
                # a single-line record the numbering stays exact; a swallowed
                # multi-line region can shift later numbers, but previews are
                # still fetched by byte offset, so the shown data is right.
                self.malformed_rows += 1
                number += 1
                continue
            yield Row(number, cells, offset)
            number += 1

    def rows(self) -> Iterator[Row]:
        """Yield data rows; the trailing run of blank lines is dropped."""
        return suppress_trailing_empty(self._iter_raw())

    def bytes_read(self) -> int:
        if self._lines is not None:
            return self._lines.next_offset
        return self._raw.tell()

    def total_bytes(self) -> int:
        return self._size

    def close(self) -> None:
        if self._text is not None:
            self._text.close()  # closes the underlying binary file too
        else:
            self._raw.close()

    def __enter__(self) -> CsvRowStream:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class CsvRowFetcher:
    """Random access to CSV rows by stored byte offset (lazy report previews)."""

    def __init__(self, path: str | Path, options: CsvOptions):
        self._options = replace(options, has_header=False)
        self._file: BinaryIO = open(path, "rb")  # noqa: SIM115 - fetcher owns the handle

    def fetch(self, offset: int) -> list[str]:
        """Re-read the single row starting at ``offset``.

        ``offset`` must be a record-start offset produced by
        :class:`CsvRowStream` (stored in the result store); arbitrary offsets
        would silently parse from mid-record.
        """
        self._file.seek(offset)
        lines = _OffsetLineIterator(self._file, self._options.encoding)
        reader = _make_reader(lines, self._options)
        return next(reader, [])

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> CsvRowFetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
