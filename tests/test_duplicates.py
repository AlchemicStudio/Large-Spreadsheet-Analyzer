"""is_duplicate: cross-row duplicate detection, grouping and combination."""

import pytest

from lsa.core.columns import ColumnRef
from lsa.core.csv_stream import CsvOptions, CsvRowStream
from lsa.core.excel_stream import OpenpyxlRowStream
from lsa.core.report import MatchRowSource
from lsa.core.rules import Condition, Rule, RuleSet, Settings
from lsa.core.scan import run_scan
from lsa.core.store import ResultStore


def _dup_rule(*columns: ColumnRef, rule_id: str = "dup", match: str = "all") -> Rule:
    return Rule(
        id=rule_id,
        label=rule_id,
        match=match,  # type: ignore[arg-type]
        conditions=(Condition("is_duplicate", columns=tuple(columns)),),
    )


def _scan(stream, *rules: Rule, settings: Settings | None = None, store=None):
    ruleset = RuleSet(rules=tuple(rules), settings=settings or Settings())
    summary = run_scan(stream, ruleset, store)
    return summary


def test_single_column_duplicates_grouped(make_csv, tmp_path) -> None:
    path = make_csv("name,email\nAna,x@a\nBo,y@b\nCy,x@a\nDee,z@c\nEve,z@c\nFin,z@c\n")
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        summary = _scan(stream, _dup_rule(ColumnRef("header", "email")), store=store)
        assert summary.counts == {"dup": 5}  # x@a group (2) + z@c group (3)
        matches = store.get_page("dup", 0, 10)
        # groups stay together, ordered by key: x@a rows (2,4) then z@c rows (5,6,7)
        assert [(m.group_key, m.row_number) for m in matches] == [
            ("x@a", 2),
            ("x@a", 4),
            ("z@c", 5),
            ("z@c", 6),
            ("z@c", 7),
        ]
        with MatchRowSource(path, CsvOptions()) as source:
            assert source.cells_for(matches[0]) == ["Ana", "x@a"]


def test_no_duplicates_no_matches(make_csv, tmp_path) -> None:
    path = make_csv("name,email\nAna,x@a\nBo,y@b\n")
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        summary = _scan(stream, _dup_rule(ColumnRef("header", "email")), store=store)
    assert summary.counts == {"dup": 0}


def test_two_column_key(make_csv, tmp_path) -> None:
    # (reason, measure) pairs: only the exact combination counts as duplicate.
    path = make_csv("id,reason,measure\n1,A,M1\n2,A,M2\n3,A,M1\n4,B,M1\n")
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        summary = _scan(
            stream,
            _dup_rule(ColumnRef("header", "reason"), ColumnRef("header", "measure")),
            store=store,
        )
        assert summary.counts == {"dup": 2}
        assert [m.row_number for m in store.get_page("dup", 0, 10)] == [2, 4]


def test_key_respects_trim_and_case_settings(make_csv, tmp_path) -> None:
    path = make_csv("id,code\n1, ABC \n2,abc\n3,xyz\n")
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r1.sqlite3") as store,
    ):
        summary = _scan(
            stream,
            _dup_rule(ColumnRef("header", "code")),
            settings=Settings(case_sensitive=False),
            store=store,
        )
        assert summary.counts == {"dup": 2}  # " ABC " and "abc" share a key
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r2.sqlite3") as store,
    ):
        summary = _scan(stream, _dup_rule(ColumnRef("header", "code")), store=store)
        assert summary.counts == {"dup": 0}  # case-sensitive: no duplicates


def test_and_with_per_row_condition(make_csv, tmp_path) -> None:
    # Duplication counts across the whole file, but AND still requires the
    # per-row conditions: the empty-name row is excluded even though its
    # email participates in a duplicate group.
    path = make_csv("name,email\nAna,x@a\n,x@a\nBo,y@b\n")
    rule = Rule(
        id="dup-named",
        label="d",
        match="all",
        conditions=(
            Condition("not_empty", ColumnRef("header", "name")),
            Condition("is_duplicate", columns=(ColumnRef("header", "email"),)),
        ),
    )
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        summary = _scan(stream, rule, store=store)
        assert summary.counts == {"dup-named": 1}
        assert [m.row_number for m in store.get_page("dup-named", 0, 10)] == [2]


def test_or_with_per_row_condition(make_csv, tmp_path) -> None:
    path = make_csv("name,email\nAna,x@a\nBo,x@a\n,z@c\n")
    rule = Rule(
        id="either",
        label="e",
        match="any",
        conditions=(
            Condition("is_empty", ColumnRef("header", "name")),
            Condition("is_duplicate", columns=(ColumnRef("header", "email"),)),
        ),
    )
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        summary = _scan(stream, rule, store=store)
        assert summary.counts == {"either": 3}
        matches = store.get_page("either", 0, 10)
        # per-row-only matches (no group) first, then groups ordered by key
        assert [(m.group_key, m.row_number) for m in matches] == [
            (None, 4),
            ("x@a", 2),
            ("x@a", 3),
        ]


def test_duplicate_and_plain_rules_together(make_csv, tmp_path) -> None:
    path = make_csv("name,email\nAna,x@a\nBo,x@a\nCy,\n")
    plain = Rule(
        id="no-email",
        label="n",
        match="all",
        conditions=(Condition("is_empty", ColumnRef("header", "email")),),
    )
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        summary = _scan(stream, _dup_rule(ColumnRef("header", "email")), plain, store=store)
    assert summary.counts == {"dup": 2, "no-email": 1}


def test_duplicates_on_xlsx_cache_cells(make_xlsx, tmp_path) -> None:
    path = make_xlsx({"Data": [["id", "code"], [1, "A"], [2, "B"], [3, "A"]]})
    stream = OpenpyxlRowStream(path)
    with ResultStore(tmp_path / "r.sqlite3") as store:
        try:
            summary = _scan(stream, _dup_rule(ColumnRef("header", "code")), store=store)
        finally:
            stream.close()
        assert summary.counts == {"dup": 2}
        matches = store.get_page("dup", 0, 10)
        assert [m.row_number for m in matches] == [2, 4]
        assert matches[0].cells == ["1", "A"]  # cached for preview


@pytest.mark.parametrize("empty_cells", [True, False])
def test_empty_cells_form_groups_too(make_csv, tmp_path, empty_cells: bool) -> None:
    # Blank cells are equal content; combine with not_empty to exclude them.
    text = "id,code\n1,\n2,\n3,x\n" if empty_cells else "id,code\n1,a\n2,b\n3,x\n"
    path = make_csv(text)
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        summary = _scan(stream, _dup_rule(ColumnRef("header", "code")), store=store)
    assert summary.counts == {"dup": 2 if empty_cells else 0}
