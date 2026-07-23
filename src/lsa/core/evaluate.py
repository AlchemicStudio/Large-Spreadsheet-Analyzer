"""Compile a RuleSet against a concrete file layout for fast per-row evaluation."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .rules import Condition, RuleSet

Predicate = Callable[[Sequence[str]], bool]
CellGetter = Callable[[Sequence[str]], str]


class CompiledRules:
    """A RuleSet bound to a header/width, evaluating every rule on each row."""

    __slots__ = ("_evaluators", "rule_ids")

    def __init__(self, rule_ids: tuple[str, ...], evaluators: tuple[Predicate, ...]):
        self.rule_ids = rule_ids
        self._evaluators = evaluators

    def evaluate(self, cells: Sequence[str]) -> list[str]:
        """Return the ids of all rules matching this row (cells as text)."""
        return [rid for rid, ev in zip(self.rule_ids, self._evaluators, strict=True) if ev(cells)]


def compile_rules(ruleset: RuleSet, header: list[str] | None, width: int) -> CompiledRules:
    """Resolve all column references and build one predicate per rule.

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

    def compile_condition(cond: Condition) -> Predicate:
        get = cell_getter(cond.column.resolve(header, width))
        if cond.type == "equals_column":
            assert cond.other_column is not None  # guaranteed by rules validation
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

    rule_ids: list[str] = []
    evaluators: list[Predicate] = []
    for rule in ruleset.rules:
        predicates = tuple(compile_condition(c) for c in rule.conditions)
        if len(predicates) == 1:
            evaluator = predicates[0]
        elif rule.match == "all":

            def evaluator(cells: Sequence[str], _p: tuple[Predicate, ...] = predicates) -> bool:
                return all(p(cells) for p in _p)

        else:

            def evaluator(cells: Sequence[str], _p: tuple[Predicate, ...] = predicates) -> bool:
                return any(p(cells) for p in _p)

        rule_ids.append(rule.id)
        evaluators.append(evaluator)
    return CompiledRules(tuple(rule_ids), tuple(evaluators))
