"""Rule model and JSON (de)serialization with validation.

A rules file looks like::

    {
      "version": 1,
      "settings": {"trim": true, "case_sensitive": true, "empty_tokens": [""]},
      "rules": [
        {
          "id": "missing-email",
          "label": "Row has a name but no e-mail",
          "match": "all",
          "conditions": [
            {"type": "not_empty", "column": {"by": "header", "value": "name"}},
            {"type": "is_empty",  "column": {"by": "header", "value": "email"}}
          ]
        }
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .columns import ColumnRef, letter_to_index

RULES_FILE_VERSION = 1

ConditionType = Literal["is_empty", "not_empty", "equals_column"]
MatchMode = Literal["all", "any"]

CONDITION_TYPES: tuple[ConditionType, ...] = ("is_empty", "not_empty", "equals_column")
MATCH_MODES: tuple[MatchMode, ...] = ("all", "any")


class RuleValidationError(ValueError):
    """A rules document is invalid; ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid rules:\n" + "\n".join(f"  - {e}" for e in errors))


@dataclass(frozen=True, slots=True)
class Settings:
    """Comparison semantics, adjustable per rules file."""

    trim: bool = True
    case_sensitive: bool = True
    empty_tokens: tuple[str, ...] = ("",)


@dataclass(frozen=True, slots=True)
class Condition:
    """One per-row condition; ``other_column`` is only set for equals_column."""

    type: ConditionType
    column: ColumnRef
    other_column: ColumnRef | None = None


@dataclass(frozen=True, slots=True)
class Rule:
    """A named group of conditions combined with AND (`all`) or OR (`any`)."""

    id: str
    label: str
    match: MatchMode
    conditions: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class RuleSet:
    """A validated rules document."""

    rules: tuple[Rule, ...]
    settings: Settings = field(default_factory=Settings)
    version: int = RULES_FILE_VERSION


def _ref_to_dict(ref: ColumnRef) -> dict[str, Any]:
    return {"by": ref.by, "value": ref.value}


def ruleset_to_dict(ruleset: RuleSet) -> dict[str, Any]:
    """Serialize a RuleSet to a JSON-compatible dict."""
    return {
        "version": ruleset.version,
        "settings": {
            "trim": ruleset.settings.trim,
            "case_sensitive": ruleset.settings.case_sensitive,
            "empty_tokens": list(ruleset.settings.empty_tokens),
        },
        "rules": [
            {
                "id": rule.id,
                "label": rule.label,
                "match": rule.match,
                "conditions": [
                    {
                        "type": cond.type,
                        "column": _ref_to_dict(cond.column),
                        **(
                            {"other_column": _ref_to_dict(cond.other_column)}
                            if cond.other_column is not None
                            else {}
                        ),
                    }
                    for cond in rule.conditions
                ],
            }
            for rule in ruleset.rules
        ],
    }


def _parse_ref(data: Any, where: str, errors: list[str]) -> ColumnRef | None:
    if not isinstance(data, dict):
        errors.append(f"{where}: column reference must be an object with 'by' and 'value'")
        return None
    by = data.get("by")
    value = data.get("value")
    if by not in ("letter", "index", "header"):
        errors.append(f"{where}: 'by' must be one of 'letter', 'index', 'header', got {by!r}")
        return None
    if by == "letter":
        if not isinstance(value, str):
            errors.append(f"{where}: a letter reference needs a string value like \"C\"")
            return None
        try:
            letter_to_index(value)
        except ValueError:
            errors.append(f"{where}: {value!r} is not a valid column letter")
            return None
        return ColumnRef("letter", value.strip().upper())
    if by == "index":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append(f"{where}: an index reference needs an integer >= 0, got {value!r}")
            return None
        return ColumnRef("index", value)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: a header reference needs a non-empty string value")
        return None
    return ColumnRef("header", value)


def _parse_settings(data: Any, errors: list[str]) -> Settings:
    if data is None:
        return Settings()
    if not isinstance(data, dict):
        errors.append("settings: must be an object")
        return Settings()
    trim = data.get("trim", True)
    case_sensitive = data.get("case_sensitive", True)
    empty_tokens = data.get("empty_tokens", [""])
    if not isinstance(trim, bool):
        errors.append(f"settings.trim: must be true or false, got {trim!r}")
        trim = True
    if not isinstance(case_sensitive, bool):
        errors.append(f"settings.case_sensitive: must be true or false, got {case_sensitive!r}")
        case_sensitive = True
    if not isinstance(empty_tokens, list) or not all(
        isinstance(t, str) for t in empty_tokens
    ):
        errors.append(f"settings.empty_tokens: must be a list of strings, got {empty_tokens!r}")
        empty_tokens = [""]
    unknown = set(data) - {"trim", "case_sensitive", "empty_tokens"}
    if unknown:
        errors.append(f"settings: unknown key(s) {sorted(unknown)}")
    return Settings(trim=trim, case_sensitive=case_sensitive, empty_tokens=tuple(empty_tokens))


def _parse_condition(data: Any, where: str, errors: list[str]) -> Condition | None:
    if not isinstance(data, dict):
        errors.append(f"{where}: must be an object")
        return None
    ctype = data.get("type")
    if ctype not in CONDITION_TYPES:
        errors.append(
            f"{where}: unknown condition type {ctype!r} "
            f"(expected one of {', '.join(CONDITION_TYPES)})"
        )
        return None
    column = _parse_ref(data.get("column"), f"{where}.column", errors)
    other: ColumnRef | None = None
    if ctype == "equals_column":
        if "other_column" not in data:
            errors.append(f"{where}: equals_column requires an 'other_column' reference")
            return None
        other = _parse_ref(data.get("other_column"), f"{where}.other_column", errors)
        if other is None:
            return None
    elif "other_column" in data:
        errors.append(f"{where}: 'other_column' is only valid for equals_column")
        return None
    if column is None:
        return None
    return Condition(type=ctype, column=column, other_column=other)


def _parse_rule(data: Any, where: str, errors: list[str]) -> Rule | None:
    if not isinstance(data, dict):
        errors.append(f"{where}: must be an object")
        return None
    rule_id = data.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        errors.append(f"{where}: 'id' must be a non-empty string")
        return None
    label = data.get("label", rule_id)
    if not isinstance(label, str) or not label.strip():
        errors.append(f"{where}: 'label' must be a non-empty string")
        label = rule_id
    match = data.get("match")
    if match not in MATCH_MODES:
        errors.append(f"{where}: 'match' must be 'all' or 'any', got {match!r}")
        return None
    raw_conditions = data.get("conditions")
    if not isinstance(raw_conditions, list) or not raw_conditions:
        errors.append(f"{where}: 'conditions' must be a non-empty list")
        return None
    conditions = [
        _parse_condition(cond, f"{where}.conditions[{i}]", errors)
        for i, cond in enumerate(raw_conditions)
    ]
    if any(c is None for c in conditions):
        return None
    return Rule(
        id=rule_id.strip(),
        label=label,
        match=match,
        conditions=tuple(c for c in conditions if c is not None),
    )


def ruleset_from_dict(data: Any) -> RuleSet:
    """Parse and validate a rules document.

    Raises :class:`RuleValidationError` listing *all* problems found.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        raise RuleValidationError(["the rules document must be a JSON object"])

    version = data.get("version")
    if version != RULES_FILE_VERSION:
        raise RuleValidationError(
            [
                f"unsupported rules file version {version!r} "
                f"(this application supports version {RULES_FILE_VERSION})"
            ]
        )

    settings = _parse_settings(data.get("settings"), errors)

    raw_rules = data.get("rules")
    rules: list[Rule] = []
    if not isinstance(raw_rules, list) or not raw_rules:
        errors.append("'rules' must be a non-empty list")
    else:
        seen_ids: set[str] = set()
        for i, raw in enumerate(raw_rules):
            rule = _parse_rule(raw, f"rules[{i}]", errors)
            if rule is None:
                continue
            if rule.id in seen_ids:
                errors.append(f"rules[{i}]: duplicate rule id {rule.id!r}")
                continue
            seen_ids.add(rule.id)
            rules.append(rule)

    if errors:
        raise RuleValidationError(errors)
    return RuleSet(rules=tuple(rules), settings=settings, version=RULES_FILE_VERSION)


def load_rules(path: str | Path) -> RuleSet:
    """Load and validate a rules JSON file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuleValidationError([f"cannot read rules file: {exc}"]) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuleValidationError([f"rules file is not valid JSON: {exc}"]) from exc
    return ruleset_from_dict(data)


def save_rules(ruleset: RuleSet, path: str | Path) -> None:
    """Write a RuleSet to a JSON file (UTF-8, pretty-printed)."""
    Path(path).write_text(
        json.dumps(ruleset_to_dict(ruleset), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
