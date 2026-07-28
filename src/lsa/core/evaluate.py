"""Compile a RuleSet against a concrete file layout for fast per-row evaluation.

Per-row conditions (is_empty / not_empty / equals_column) become one
predicate per rule.  ``is_duplicate`` conditions cannot be decided per row —
they need knowledge of the whole file — so compilation exposes them as *key
extractors*: the scanner records every row's key(s) and resolves duplicate
groups after the pass (see :mod:`lsa.core.scan`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import NamedTuple

from .rules import Condition, RuleSet

Predicate = Callable[[Sequence[str]], bool]
CellGetter = Callable[[Sequence[str]], str]
KeyExtractor = Callable[[Sequence[str]], str]

# Joins multi-column duplicate keys; the unit separator cannot appear in
# cell text coming from the readers.
_KEY_SEPARATOR = "\x1f"


class DuplicateCondition(NamedTuple):
    """One is_duplicate condition bound to the file layout.

    ``right_key_of`` extracts the row's cells to the right of the rightmost
    key column: matched rows are sub-grouped by that content in the report
    (identical right-hand rows together, unique ones in a final group).
    """

    cond_id: str
    key_of: KeyExtractor
    right_key_of: KeyExtractor


class CompiledRule(NamedTuple):
    """One rule bound to the file layout.

    ``perrow`` is None when the rule has no per-row conditions (pure
    duplicate rule); ``dup_conds`` is empty for ordinary rules.
    """

    rule_id: str
    match: str
    perrow: Predicate | None
    dup_conds: tuple[DuplicateCondition, ...]


class CompiledRules:
    """A RuleSet bound to a header/width, evaluating every rule on each row."""

    __slots__ = ("_plain", "has_duplicates", "rule_ids", "rules")

    def __init__(self, rules: tuple[CompiledRule, ...]):
        self.rules = rules
        self.rule_ids = tuple(rule.rule_id for rule in rules)
        self.has_duplicates = any(rule.dup_conds for rule in rules)
        # Fast path used when no rule involves duplicate detection.
        self._plain = tuple((rule.rule_id, rule.perrow) for rule in rules if not rule.dup_conds)

    def evaluate(self, cells: Sequence[str]) -> list[str]:
        """Ids of rules (without duplicate conditions) matching this row."""
        return [rule_id for rule_id, perrow in self._plain if perrow is not None and perrow(cells)]


def compile_rules(ruleset: RuleSet, header: list[str] | None, width: int) -> CompiledRules:
    """Resolve all column references and build one CompiledRule per rule.

    ``header``/``width`` come from the file being scanned (see
    :meth:`lsa.core.columns.ColumnRef.resolve`).  Raises
    :class:`lsa.core.columns.ColumnResolutionError` on unresolvable references.
    """
    settings = ruleset.settings
    trim = settings.trim
    fold: Callable[[str], str] | None = None if settings.case_sensitive else str.casefold

    tokens = frozenset(
        (t.strip() if trim else t) if fold is None else fold(t.strip() if trim else t)
        for t in settings.empty_tokens
    )

    def cell_getter(idx: int) -> CellGetter:
        # A missing cell (short/ragged row) reads as "".
        if trim:

            def get(cells: Sequence[str], _i: int = idx) -> str:
                return cells[_i].strip() if _i < len(cells) else ""

        else:

            def get(cells: Sequence[str], _i: int = idx) -> str:
                return cells[_i] if _i < len(cells) else ""

        return get

    def transformed_getter(idx: int) -> CellGetter:
        get = cell_getter(idx)
        if fold is None:
            return get
        return lambda cells: fold(get(cells))

    def compile_condition(cond: Condition) -> Predicate:
        assert cond.column is not None  # guaranteed by rules validation
        get = cell_getter(cond.column.resolve(header, width))
        if cond.type == "equals_column":
            assert cond.other_column is not None
            other = cell_getter(cond.other_column.resolve(header, width))
            if fold is None:
                return lambda cells: get(cells) == other(cells)
            return lambda cells: fold(get(cells)) == fold(other(cells))
        if fold is None:
            empty: Predicate = lambda cells: get(cells) in tokens  # noqa: E731
        else:
            empty = lambda cells: fold(get(cells)) in tokens  # noqa: E731
        if cond.type == "is_empty":
            return empty
        return lambda cells: not empty(cells)

    def compile_duplicate(rule_id: str, index: int, cond: Condition) -> DuplicateCondition:
        indexes = tuple(ref.resolve(header, width) for ref in cond.columns or ())
        getters = tuple(transformed_getter(i) for i in indexes)
        right_start = max(indexes) + 1

        if len(getters) == 1:
            key_of: KeyExtractor = getters[0]
        else:

            def key_of(cells: Sequence[str], _g: tuple[CellGetter, ...] = getters) -> str:
                return _KEY_SEPARATOR.join(g(cells) for g in _g)

        def right_key_of(cells: Sequence[str], _start: int = right_start) -> str:
            parts = []
            for value in cells[_start:]:
                if trim:
                    value = value.strip()
                parts.append(value if fold is None else fold(value))
            # Padding-insensitive: a missing trailing cell and an empty one
            # are the same content (a ragged source row and its padded copy
            # in the duplicate file must produce identical keys).
            while parts and parts[-1] == "":
                parts.pop()
            return _KEY_SEPARATOR.join(parts)

        return DuplicateCondition(f"{rule_id}{_KEY_SEPARATOR}{index}", key_of, right_key_of)

    compiled: list[CompiledRule] = []
    for rule in ruleset.rules:
        predicates: list[Predicate] = []
        dup_conds: list[DuplicateCondition] = []
        for index, cond in enumerate(rule.conditions):
            if cond.type == "is_duplicate":
                dup_conds.append(compile_duplicate(rule.id, index, cond))
            else:
                predicates.append(compile_condition(cond))

        perrow: Predicate | None
        if not predicates:
            perrow = None
        elif len(predicates) == 1:
            perrow = predicates[0]
        elif rule.match == "all":

            def perrow(cells: Sequence[str], _p: tuple[Predicate, ...] = tuple(predicates)) -> bool:
                return all(p(cells) for p in _p)

        else:

            def perrow(cells: Sequence[str], _p: tuple[Predicate, ...] = tuple(predicates)) -> bool:
                return any(p(cells) for p in _p)

        compiled.append(
            CompiledRule(
                rule_id=rule.id,
                match=rule.match,
                perrow=perrow,
                dup_conds=tuple(dup_conds),
            )
        )
    return CompiledRules(tuple(compiled))
