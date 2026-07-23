"""Column letter conversion and reference resolution."""

import pytest

from lsa.core.columns import (
    ColumnRef,
    ColumnResolutionError,
    index_to_letter,
    letter_to_index,
)

HEADER = ["name", "email", " city ", "email2"]


@pytest.mark.parametrize(
    ("letter", "index"),
    [("A", 0), ("B", 1), ("Z", 25), ("AA", 26), ("AZ", 51), ("BA", 52), ("ZZ", 701), ("AAA", 702)],
)
def test_letter_index_round_trip(letter: str, index: int) -> None:
    assert letter_to_index(letter) == index
    assert index_to_letter(index) == letter


def test_letter_is_case_insensitive_and_trimmed() -> None:
    assert letter_to_index(" aa ") == 26


@pytest.mark.parametrize("bad", ["", "1", "A1", "É", "A B", "-"])
def test_invalid_letters_raise(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid column letter"):
        letter_to_index(bad)


def test_index_to_letter_rejects_negative() -> None:
    with pytest.raises(ValueError):
        index_to_letter(-1)


def test_resolve_by_letter_and_index() -> None:
    assert ColumnRef("letter", "C").resolve(HEADER, 4) == 2
    assert ColumnRef("index", 3).resolve(HEADER, 4) == 3
    assert ColumnRef("index", 3).resolve(None, 4) == 3  # headerless files work too


def test_resolve_by_header_trims_both_sides() -> None:
    assert ColumnRef("header", "city").resolve(HEADER, 4) == 2
    assert ColumnRef("header", " email ").resolve(HEADER, 4) == 1


def test_resolve_header_without_header_row_fails() -> None:
    with pytest.raises(ColumnResolutionError, match="without a header row"):
        ColumnRef("header", "email").resolve(None, 4)


def test_resolve_unknown_header_lists_available() -> None:
    with pytest.raises(ColumnResolutionError, match=r"no such header.*'name'"):
        ColumnRef("header", "phone").resolve(HEADER, 4)


def test_resolve_ambiguous_header_fails() -> None:
    header = ["email", "name", "email"]
    with pytest.raises(ColumnResolutionError, match=r"ambiguous.*columns A, C"):
        ColumnRef("header", "email").resolve(header, 3)


@pytest.mark.parametrize("ref", [ColumnRef("letter", "ZZ"), ColumnRef("index", 4)])
def test_resolve_out_of_range_fails(ref: ColumnRef) -> None:
    with pytest.raises(ColumnResolutionError, match="out of range"):
        ref.resolve(HEADER, 4)


def test_resolve_out_of_range_on_empty_file() -> None:
    with pytest.raises(ColumnResolutionError, match="empty"):
        ColumnRef("index", 0).resolve(None, 0)
