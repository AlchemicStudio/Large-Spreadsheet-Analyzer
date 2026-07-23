"""Rules JSON parsing, validation and round-trip."""

import json
from pathlib import Path

import pytest

from lsa.core.columns import ColumnRef
from lsa.core.rules import (
    Condition,
    Rule,
    RuleSet,
    RuleValidationError,
    Settings,
    load_rules,
    ruleset_from_dict,
    ruleset_to_dict,
    save_rules,
)

VALID = {
    "version": 1,
    "settings": {"trim": True, "case_sensitive": True, "empty_tokens": ["", "NA"]},
    "rules": [
        {
            "id": "missing-email",
            "label": "Row has a name but no e-mail",
            "match": "all",
            "conditions": [
                {"type": "not_empty", "column": {"by": "header", "value": "name"}},
                {"type": "is_empty", "column": {"by": "header", "value": "email"}},
            ],
        },
        {
            "id": "billing-equals-shipping",
            "label": "Billing equals shipping",
            "match": "any",
            "conditions": [
                {
                    "type": "equals_column",
                    "column": {"by": "letter", "value": "D"},
                    "other_column": {"by": "letter", "value": "H"},
                }
            ],
        },
    ],
}


def test_parse_valid_document() -> None:
    rs = ruleset_from_dict(VALID)
    assert [r.id for r in rs.rules] == ["missing-email", "billing-equals-shipping"]
    assert rs.settings.empty_tokens == ("", "NA")
    assert rs.rules[0].match == "all"
    assert rs.rules[0].conditions[0].column == ColumnRef("header", "name")
    assert rs.rules[1].conditions[0].other_column == ColumnRef("letter", "H")


def test_round_trip_via_file(tmp_path: Path) -> None:
    rs = ruleset_from_dict(VALID)
    path = tmp_path / "rules.json"
    save_rules(rs, path)
    assert load_rules(path) == rs
    # and the on-disk form is the canonical dict form
    assert json.loads(path.read_text(encoding="utf-8")) == ruleset_to_dict(rs)


def test_label_defaults_to_id() -> None:
    doc = {
        "version": 1,
        "rules": [
            {
                "id": "r1",
                "match": "all",
                "conditions": [{"type": "is_empty", "column": {"by": "index", "value": 0}}],
            }
        ],
    }
    rs = ruleset_from_dict(doc)
    assert rs.rules[0].label == "r1"
    assert rs.settings == Settings()


def _expect_errors(doc: object, *fragments: str) -> None:
    with pytest.raises(RuleValidationError) as exc_info:
        ruleset_from_dict(doc)
    text = "\n".join(exc_info.value.errors)
    for fragment in fragments:
        assert fragment in text, f"expected {fragment!r} in:\n{text}"


def test_wrong_version_rejected() -> None:
    _expect_errors({"version": 2, "rules": []}, "unsupported rules file version 2")
    _expect_errors({"rules": []}, "unsupported rules file version None")


def test_not_an_object_rejected() -> None:
    _expect_errors([], "must be a JSON object")


def test_missing_or_empty_rules_rejected() -> None:
    _expect_errors({"version": 1}, "'rules' must be a non-empty list")
    _expect_errors({"version": 1, "rules": []}, "'rules' must be a non-empty list")


def test_bad_match_mode_and_missing_id() -> None:
    doc = {
        "version": 1,
        "rules": [
            {"id": "x", "match": "nand", "conditions": [{}]},
            {"match": "all", "conditions": [{}]},
        ],
    }
    _expect_errors(doc, "rules[0]: 'match' must be 'all' or 'any'", "rules[1]: 'id' must be")


def test_duplicate_rule_ids_rejected() -> None:
    rule = {
        "id": "dup",
        "match": "all",
        "conditions": [{"type": "is_empty", "column": {"by": "index", "value": 0}}],
    }
    _expect_errors({"version": 1, "rules": [rule, dict(rule)]}, "duplicate rule id 'dup'")


def test_unknown_condition_type_rejected() -> None:
    doc = {
        "version": 1,
        "rules": [
            {
                "id": "r",
                "match": "all",
                "conditions": [{"type": "regex", "column": {"by": "index", "value": 0}}],
            }
        ],
    }
    _expect_errors(doc, "rules[0].conditions[0]: unknown condition type 'regex'")


def test_equals_column_requires_other_column() -> None:
    doc = {
        "version": 1,
        "rules": [
            {
                "id": "r",
                "match": "all",
                "conditions": [{"type": "equals_column", "column": {"by": "index", "value": 0}}],
            }
        ],
    }
    _expect_errors(doc, "equals_column requires an 'other_column'")


def test_other_column_forbidden_elsewhere() -> None:
    doc = {
        "version": 1,
        "rules": [
            {
                "id": "r",
                "match": "all",
                "conditions": [
                    {
                        "type": "is_empty",
                        "column": {"by": "index", "value": 0},
                        "other_column": {"by": "index", "value": 1},
                    }
                ],
            }
        ],
    }
    _expect_errors(doc, "'other_column' is only valid for equals_column")


@pytest.mark.parametrize(
    ("ref", "fragment"),
    [
        ({"by": "letter", "value": "12"}, "not a valid column letter"),
        ({"by": "letter", "value": 3}, "needs a string value"),
        ({"by": "index", "value": -1}, "integer >= 0"),
        ({"by": "index", "value": True}, "integer >= 0"),
        ({"by": "index", "value": "0"}, "integer >= 0"),
        ({"by": "header", "value": ""}, "non-empty string"),
        ({"by": "position", "value": 0}, "'by' must be one of"),
        ("C", "must be an object"),
    ],
)
def test_bad_column_refs_rejected(ref: object, fragment: str) -> None:
    doc = {
        "version": 1,
        "rules": [{"id": "r", "match": "all", "conditions": [{"type": "is_empty", "column": ref}]}],
    }
    _expect_errors(doc, fragment)


def test_column_letter_normalized_to_upper() -> None:
    doc = {
        "version": 1,
        "rules": [
            {
                "id": "r",
                "match": "all",
                "conditions": [{"type": "is_empty", "column": {"by": "letter", "value": "aa"}}],
            }
        ],
    }
    assert ruleset_from_dict(doc).rules[0].conditions[0].column == ColumnRef("letter", "AA")


def test_bad_settings_reported() -> None:
    doc = {
        "version": 1,
        "settings": {"trim": "yes", "empty_tokens": ["", 0], "surprise": 1},
        "rules": VALID["rules"],
    }
    _expect_errors(doc, "settings.trim", "settings.empty_tokens", "unknown key(s) ['surprise']")


def test_load_rules_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RuleValidationError, match="cannot read rules file"):
        load_rules(tmp_path / "nope.json")


def test_load_rules_reports_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuleValidationError, match="not valid JSON"):
        load_rules(path)


def test_programmatic_ruleset_serializes() -> None:
    rs = RuleSet(
        rules=(
            Rule(
                id="r1",
                label="L",
                match="any",
                conditions=(Condition("not_empty", ColumnRef("index", 0)),),
            ),
        )
    )
    assert ruleset_from_dict(ruleset_to_dict(rs)) == rs
