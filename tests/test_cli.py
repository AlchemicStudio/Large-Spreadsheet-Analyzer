"""CLI smoke tests: exit codes, summary output, report/export side effects."""

import json
from pathlib import Path

import pytest

from lsa.cli.main import main

RULES = {
    "version": 1,
    "settings": {"trim": True, "case_sensitive": True, "empty_tokens": [""]},
    "rules": [
        {
            "id": "no-email",
            "label": "Missing e-mail",
            "match": "all",
            "conditions": [
                {"type": "not_empty", "column": {"by": "header", "value": "name"}},
                {"type": "is_empty", "column": {"by": "header", "value": "email"}},
            ],
        }
    ],
}


@pytest.fixture
def rules_file(tmp_path: Path) -> Path:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(RULES), encoding="utf-8")
    return path


def _run(*extra: str, file: Path, rules: Path) -> int:
    return main(["run", "--file", str(file), "--rules", str(rules), "--no-progress", *extra])


def test_matches_found_exits_1(make_csv, rules_file, capsys) -> None:
    data = make_csv("name,email\nAna,\nBo,b@c.d\n")
    assert _run("--header", file=data, rules=rules_file) == 1
    out = capsys.readouterr().out
    assert "Scanned 2 rows" in out
    assert "no-email: 1 match(es)" in out


def test_no_matches_exits_0(make_csv, rules_file) -> None:
    data = make_csv("name,email\nAna,a@b.c\n")
    assert _run("--header", file=data, rules=rules_file) == 0


def test_missing_file_exits_2(rules_file, tmp_path, capsys) -> None:
    assert _run(file=tmp_path / "nope.csv", rules=rules_file) == 2
    assert "file not found" in capsys.readouterr().err


def test_unsupported_extension_exits_2(tmp_path, rules_file, capsys) -> None:
    path = tmp_path / "data.parquet"
    path.write_text("x", encoding="utf-8")
    assert _run(file=path, rules=rules_file) == 2
    assert "unsupported file type" in capsys.readouterr().err


def test_invalid_rules_exit_2(make_csv, tmp_path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"version": 1, "rules": []}', encoding="utf-8")
    data = make_csv("a,b\n1,2\n")
    assert _run(file=data, rules=bad) == 2
    assert "non-empty list" in capsys.readouterr().err


def test_unresolvable_column_exits_2(make_csv, rules_file, capsys) -> None:
    data = make_csv("foo,bar\n1,2\n")
    assert _run("--header", file=data, rules=rules_file) == 2
    assert "no such header" in capsys.readouterr().err


def test_unknown_sheet_exits_2(make_xlsx, rules_file, capsys) -> None:
    data = make_xlsx({"Data": [["name", "email"], ["Ana", None]]})
    assert _run("--sheet", "Nope", file=data, rules=rules_file) == 2
    assert "no sheet named" in capsys.readouterr().err


def test_report_and_export(make_csv, rules_file, tmp_path, capsys) -> None:
    data = make_csv("name,email\nAna,\nBo,\nCy,c@d.e\n")
    report = tmp_path / "report.json"
    out_dir = tmp_path / "exports"
    code = _run(
        "--header",
        "--report",
        str(report),
        "--export-matches",
        str(out_dir),
        "--sample",
        "1",
        file=data,
        rules=rules_file,
    )
    assert code == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["rows_scanned"] == 3
    assert payload["rules"][0]["match_count"] == 2
    assert payload["rules"][0]["sample_row_numbers"] == [2]
    exported = (out_dir / "no-email.csv").read_text(encoding="utf-8").splitlines()
    assert exported[0] == "row_number,name,email"
    assert len(exported) == 3


def test_empty_token_override(make_csv, rules_file) -> None:
    data = make_csv("name,email\nAna,NA\n")
    assert _run("--header", file=data, rules=rules_file) == 0
    assert _run("--header", "--empty-token", "NA", file=data, rules=rules_file) == 1


def test_separator_and_no_header(make_csv, tmp_path) -> None:
    rules = tmp_path / "letters.json"
    rules.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "id": "b-empty",
                        "label": "col B empty",
                        "match": "all",
                        "conditions": [
                            {"type": "is_empty", "column": {"by": "letter", "value": "B"}}
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    data = make_csv("Ana;\nBo;x\n")
    assert _run("--separator", ";", "--no-header", file=data, rules=rules) == 1


def test_xlsx_run(make_xlsx, rules_file, capsys) -> None:
    data = make_xlsx({"Data": [["name", "email"], ["Ana", None], ["Bo", "b@c.d"]]})
    assert _run(file=data, rules=rules_file) == 1
    assert "no-email: 1 match(es)" in capsys.readouterr().out


def test_validate_rules_ok(rules_file, capsys) -> None:
    assert main(["validate-rules", str(rules_file)]) == 0
    assert "OK: 1 rule(s)" in capsys.readouterr().out


def test_validate_rules_bad(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"version": 99}', encoding="utf-8")
    assert main(["validate-rules", str(bad)]) == 2
    assert "unsupported rules file version" in capsys.readouterr().err


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
