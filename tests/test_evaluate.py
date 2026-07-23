"""Rule evaluation semantics: condition types, AND/OR, trim, case, empty tokens."""

import pytest

from lsa.core.columns import ColumnRef, ColumnResolutionError
from lsa.core.evaluate import compile_rules
from lsa.core.rules import Condition, Rule, RuleSet, Settings

HEADER = ["name", "email", "billing", "shipping"]


def _rule(match: str, *conditions: Condition, rule_id: str = "r") -> Rule:
    return Rule(id=rule_id, label=rule_id, match=match, conditions=tuple(conditions))  # type: ignore[arg-type]


def _compile(*rules: Rule, settings: Settings | None = None, header: list[str] | None = None):
    header = HEADER if header is None else header
    return compile_rules(
        RuleSet(rules=tuple(rules), settings=settings or Settings()), header, len(header)
    )


def is_empty(col: str) -> Condition:
    return Condition("is_empty", ColumnRef("header", col))


def not_empty(col: str) -> Condition:
    return Condition("not_empty", ColumnRef("header", col))


def equals(col: str, other: str) -> Condition:
    return Condition("equals_column", ColumnRef("header", col), ColumnRef("header", other))


def test_is_empty_and_not_empty() -> None:
    compiled = _compile(_rule("all", is_empty("email")))
    assert compiled.evaluate(["Ana", "", "x", "y"]) == ["r"]
    assert compiled.evaluate(["Ana", "a@b.c", "x", "y"]) == []

    compiled = _compile(_rule("all", not_empty("email")))
    assert compiled.evaluate(["Ana", "a@b.c", "x", "y"]) == ["r"]
    assert compiled.evaluate(["Ana", "", "x", "y"]) == []


def test_whitespace_only_is_empty_when_trimming() -> None:
    compiled = _compile(_rule("all", is_empty("email")))
    assert compiled.evaluate(["Ana", "   ", "x", "y"]) == ["r"]


def test_trim_disabled_keeps_whitespace() -> None:
    compiled = _compile(_rule("all", is_empty("email")), settings=Settings(trim=False))
    assert compiled.evaluate(["Ana", "   ", "x", "y"]) == []
    assert compiled.evaluate(["Ana", "", "x", "y"]) == ["r"]


def test_custom_empty_tokens() -> None:
    settings = Settings(empty_tokens=("", "NA", "-"))
    compiled = _compile(_rule("all", is_empty("email")), settings=settings)
    for cell in ["", "NA", "-", " NA "]:
        assert compiled.evaluate(["Ana", cell, "x", "y"]) == ["r"], cell
    assert compiled.evaluate(["Ana", "na", "x", "y"]) == []  # case-sensitive by default


def test_empty_tokens_case_insensitive() -> None:
    settings = Settings(empty_tokens=("", "NA"), case_sensitive=False)
    compiled = _compile(_rule("all", is_empty("email")), settings=settings)
    assert compiled.evaluate(["Ana", "na", "x", "y"]) == ["r"]


def test_missing_cells_in_short_rows_are_empty() -> None:
    compiled = _compile(_rule("all", is_empty("shipping")))
    assert compiled.evaluate(["Ana"]) == ["r"]
    assert compiled.evaluate([]) == ["r"]


def test_equals_column() -> None:
    compiled = _compile(_rule("all", equals("billing", "shipping")))
    assert compiled.evaluate(["a", "b", "12 Main St", "12 Main St"]) == ["r"]
    assert compiled.evaluate(["a", "b", "12 Main St", "34 Oak Ave"]) == []
    # trimmed before comparison
    assert compiled.evaluate(["a", "b", " 12 Main St ", "12 Main St"]) == ["r"]
    # two empty cells are equal
    assert compiled.evaluate(["a", "b", "", ""]) == ["r"]


def test_equals_column_case_sensitivity() -> None:
    compiled = _compile(_rule("all", equals("billing", "shipping")))
    assert compiled.evaluate(["a", "b", "Main", "main"]) == []
    compiled = _compile(
        _rule("all", equals("billing", "shipping")), settings=Settings(case_sensitive=False)
    )
    assert compiled.evaluate(["a", "b", "Main", "MAIN"]) == ["r"]


def test_all_requires_every_condition() -> None:
    compiled = _compile(_rule("all", not_empty("name"), is_empty("email")))
    assert compiled.evaluate(["Ana", "", "x", "y"]) == ["r"]
    assert compiled.evaluate(["", "", "x", "y"]) == []
    assert compiled.evaluate(["Ana", "a@b.c", "x", "y"]) == []


def test_any_requires_one_condition() -> None:
    compiled = _compile(_rule("any", is_empty("name"), is_empty("email")))
    assert compiled.evaluate(["", "a@b.c", "x", "y"]) == ["r"]
    assert compiled.evaluate(["Ana", "", "x", "y"]) == ["r"]
    assert compiled.evaluate(["Ana", "a@b.c", "x", "y"]) == []


def test_rules_evaluate_independently() -> None:
    compiled = _compile(
        _rule("all", is_empty("email"), rule_id="no-email"),
        _rule("all", equals("billing", "shipping"), rule_id="same-addr"),
    )
    assert compiled.evaluate(["Ana", "", "X", "X"]) == ["no-email", "same-addr"]
    assert compiled.evaluate(["Ana", "", "X", "Y"]) == ["no-email"]
    assert compiled.evaluate(["Ana", "a@b.c", "X", "X"]) == ["same-addr"]
    assert compiled.evaluate(["Ana", "a@b.c", "X", "Y"]) == []


def test_mixed_reference_kinds_resolve() -> None:
    rule = _rule(
        "all",
        Condition("equals_column", ColumnRef("letter", "C"), ColumnRef("index", 3)),
    )
    compiled = _compile(rule)
    assert compiled.evaluate(["a", "b", "X", "X"]) == ["r"]


def test_compile_surfaces_resolution_errors() -> None:
    with pytest.raises(ColumnResolutionError, match="no such header"):
        _compile(_rule("all", is_empty("phone")))
