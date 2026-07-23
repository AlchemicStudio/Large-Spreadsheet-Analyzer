"""Column references and their resolution against a concrete file layout."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

RefBy = Literal["letter", "index", "header"]

_LETTER_RE = re.compile(r"^[A-Z]+$")


class ColumnResolutionError(ValueError):
    """A column reference cannot be resolved against the scanned file."""


def letter_to_index(letter: str) -> int:
    """Convert a spreadsheet column letter ("A", "B", ..., "AA") to a 0-based index."""
    normalized = letter.strip().upper()
    if not _LETTER_RE.match(normalized):
        raise ValueError(f"invalid column letter: {letter!r}")
    index = 0
    for char in normalized:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def index_to_letter(index: int) -> str:
    """Convert a 0-based column index to a spreadsheet letter ("A", "B", ..., "AA")."""
    if index < 0:
        raise ValueError(f"column index must be >= 0, got {index}")
    letters = ""
    remaining = index + 1
    while remaining:
        remaining, digit = divmod(remaining - 1, 26)
        letters = chr(ord("A") + digit) + letters
    return letters


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """A column referenced by letter ("C"), 0-based index (2) or header name ("email")."""

    by: RefBy
    value: str | int

    def describe(self) -> str:
        """Human-readable form for error messages."""
        if self.by == "letter":
            return f"column {self.value}"
        if self.by == "index":
            return f"column index {self.value}"
        return f"column with header {self.value!r}"

    def resolve(self, header: list[str] | None, width: int) -> int:
        """Return the 0-based index of this column in the scanned file.

        ``header`` is the (raw) header row, or ``None`` for headerless files.
        ``width`` is the number of columns of the header row (or of the first
        data row for headerless files); letter/index references beyond it are
        rejected so that a typo does not silently match empty cells everywhere.

        Raises :class:`ColumnResolutionError` with a user-facing message.
        """
        if self.by == "header":
            if header is None:
                raise ColumnResolutionError(
                    f"{self.describe()}: the rules reference columns by header name, "
                    "but the file was imported without a header row"
                )
            wanted = str(self.value).strip()
            hits = [i for i, cell in enumerate(header) if cell.strip() == wanted]
            if not hits:
                raise ColumnResolutionError(
                    f"{self.describe()}: no such header in the file "
                    f"(available: {', '.join(repr(h) for h in header)})"
                )
            if len(hits) > 1:
                letters = ", ".join(index_to_letter(i) for i in hits)
                raise ColumnResolutionError(
                    f"{self.describe()}: ambiguous, found in columns {letters}; "
                    "reference it by letter or index instead"
                )
            return hits[0]

        if self.by == "letter":
            try:
                index = letter_to_index(str(self.value))
            except ValueError as exc:
                raise ColumnResolutionError(str(exc)) from exc
        else:
            index = int(self.value)  # validated as int >= 0 on load

        if index >= width:
            raise ColumnResolutionError(
                f"{self.describe()} is out of range: the file only has "
                f"{width} column(s) (last is {index_to_letter(width - 1)})"
                if width
                else f"{self.describe()} is out of range: the file appears to be empty"
            )
        return index
