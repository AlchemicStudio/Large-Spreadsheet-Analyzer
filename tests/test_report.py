"""Report model, pager, row source and exports."""

import csv
import json

from lsa.core.columns import ColumnRef
from lsa.core.csv_stream import CsvOptions, CsvRowStream
from lsa.core.report import (
    MatchPager,
    MatchRowSource,
    build_report,
    export_all_matches,
    export_rule_matches,
    report_to_dict,
    safe_filename,
    save_report,
)
from lsa.core.rules import Condition, Rule, RuleSet
from lsa.core.scan import run_scan
from lsa.core.store import ResultStore

RULESET = RuleSet(
    rules=(
        Rule(
            id="no-email",
            label="Missing e-mail",
            match="all",
            conditions=(Condition("is_empty", ColumnRef("header", "email")),),
        ),
        Rule(
            id="never-matches",
            label="Empty name",
            match="all",
            conditions=(Condition("is_empty", ColumnRef("header", "name")),),
        ),
    )
)

CSV_TEXT = "name,email\nAna,\nBo,b@c.d\nCy,\nDee,\nEve,\n"


def _scan(make_csv, tmp_path):
    path = make_csv(CSV_TEXT)
    store = ResultStore(tmp_path / "r.sqlite3")
    with CsvRowStream(path, CsvOptions()) as stream:
        summary = run_scan(stream, RULESET, store)
        header, width = stream.header, stream.width
    return path, store, summary, header, width


def test_pager_navigation(make_csv, tmp_path) -> None:
    _, store, _, _, _ = _scan(make_csv, tmp_path)
    with store:
        pager = MatchPager(store, "no-email", page_size=3)
        assert pager.total == 4
        assert [m.row_number for m in pager.page()] == [2, 4, 5]
        assert pager.position_label == "1-3 of 4"
        assert pager.has_next and not pager.has_prev
        assert [m.row_number for m in pager.next()] == [6]
        assert pager.position_label == "4-4 of 4"
        assert not pager.has_next and pager.has_prev
        assert [m.row_number for m in pager.next()] == [6]  # clamped at the end
        assert [m.row_number for m in pager.prev()] == [2, 4, 5]
        assert [m.row_number for m in pager.prev()] == [2, 4, 5]  # clamped at start


def test_pager_empty_rule(make_csv, tmp_path) -> None:
    _, store, _, _, _ = _scan(make_csv, tmp_path)
    with store:
        pager = MatchPager(store, "never-matches")
        assert pager.total == 0
        assert pager.page() == []
        assert pager.position_label == "0 of 0"
        assert not pager.has_next and not pager.has_prev


def test_pager_page_size_change(make_csv, tmp_path) -> None:
    _, store, _, _, _ = _scan(make_csv, tmp_path)
    with store:
        pager = MatchPager(store, "no-email", page_size=1)
        pager.next()
        pager.next()  # offset 2
        assert [m.row_number for m in pager.set_page_size(2)] == [5, 6]


def test_match_row_source_reads_csv_lazily(make_csv, tmp_path) -> None:
    path, store, _, _, _ = _scan(make_csv, tmp_path)
    with store, MatchRowSource(path, CsvOptions()) as source:
        rows = [source.cells_for(m) for m in store.get_page("no-email", 0, 2)]
    assert rows == [["Ana", ""], ["Cy", ""]]


def test_report_build_and_json_round_trip(make_csv, tmp_path) -> None:
    path, store, summary, _, _ = _scan(make_csv, tmp_path)
    with store:
        report = build_report(
            file=path, sheet=None, ruleset=RULESET, summary=summary, store=store, sample_size=2
        )
    assert report.rows_scanned == 5
    assert report.partial is False
    by_id = {r.rule_id: r for r in report.rules}
    assert by_id["no-email"].match_count == 4
    assert by_id["no-email"].sample_row_numbers == (2, 4)
    assert by_id["never-matches"].match_count == 0

    out = tmp_path / "report.json"
    save_report(report, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == report_to_dict(report)
    assert data["version"] == 1
    assert data["rules"][0]["id"] == "no-email"


def test_export_rule_matches_with_header(make_csv, tmp_path) -> None:
    path, store, _, header, width = _scan(make_csv, tmp_path)
    out = tmp_path / "no-email.csv"
    with store, MatchRowSource(path, CsvOptions()) as source:
        written = export_rule_matches(store, "no-email", source, out, header=header, width=width)
    assert written == 4
    with open(out, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["row_number", "name", "email"]
    assert rows[1] == ["2", "Ana", ""]


def test_export_headerless_uses_letters(make_csv, tmp_path) -> None:
    path = make_csv("Ana,\nBo,b@c.d\n", name="plain.csv")
    ruleset = RuleSet(
        rules=(
            Rule(
                id="r",
                label="r",
                match="all",
                conditions=(Condition("is_empty", ColumnRef("letter", "B")),),
            ),
        )
    )
    options = CsvOptions(has_header=False)
    with (
        CsvRowStream(path, options) as stream,
        ResultStore(tmp_path / "r2.sqlite3") as store,
    ):
        run_scan(stream, ruleset, store)
        out = tmp_path / "out.csv"
        with MatchRowSource(path, options) as source:
            export_rule_matches(store, "r", source, out, header=None, width=stream.width)
    with open(out, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows == [["row_number", "A", "B"], ["1", "Ana", ""]]


def test_export_all_matches(make_csv, tmp_path) -> None:
    path, store, _, header, width = _scan(make_csv, tmp_path)
    out_dir = tmp_path / "exports"
    with store, MatchRowSource(path, CsvOptions()) as source:
        files = export_all_matches(store, RULESET, source, out_dir, header=header, width=width)
    assert set(files) == {"no-email", "never-matches"}
    assert files["no-email"].name == "no-email.csv"
    empty = files["never-matches"].read_text(encoding="utf-8").strip().splitlines()
    assert empty == ["row_number,name,email"]


def test_export_all_dedupes_colliding_filenames(make_csv, tmp_path) -> None:
    # Distinct rule ids can sanitize to the same file name; neither export
    # may silently overwrite the other.
    condition = Condition("is_empty", ColumnRef("header", "email"))
    ruleset = RuleSet(
        rules=(
            Rule(id="no email", label="a", match="all", conditions=(condition,)),
            Rule(id="no_email", label="b", match="all", conditions=(condition,)),
        )
    )
    path = make_csv(CSV_TEXT)
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r3.sqlite3") as store,
    ):
        run_scan(stream, ruleset, store)
        with MatchRowSource(path, CsvOptions()) as source:
            files = export_all_matches(
                store, ruleset, source, tmp_path / "dup", header=stream.header, width=stream.width
            )
    assert files["no email"].name == "no_email.csv"
    assert files["no_email"].name == "no_email-2.csv"
    assert files["no email"].exists() and files["no_email"].exists()


def test_safe_filename() -> None:
    assert safe_filename("no-email") == "no-email"
    assert safe_filename("weird/rule: 1") == "weird_rule_1"
    assert safe_filename("///") == "rule"
