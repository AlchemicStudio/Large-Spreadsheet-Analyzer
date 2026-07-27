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


USER_EXAMPLE = """\
12309890;716G67G62;AMM;O009KH;1;4;3
129372;716G67G62;UTYA;HSQ65;1;1;3
92837;716G67G62;AMM;O009KH;1;2;3
39384;716G67G62;AMM;AA12;2;2;2
33290;716G67G62;PAQPZ;12KKL;1;5;3
9875789;716G67G62;AMM;O009KH;3;3;8
129372;716G67G62;2SSYA;HSQ65;1;2;3
92837;716G67G62;AMM;O009KH;1;2;3
39384;716G67G62;AMM;2HIZA;2;4;2
102938;716G67G62;AMM;O009KH;1;2;2
33290;716G67G62;46JYT;112JGH;1;2;3
129372;716G67G62;UTAAYA;HAASQ65;1;1;3
23293;716G67G62;AMM;O009KH;3;3;8
39384;716G67G62;P2P2;WXS23;2;2;2
92837;716G67G62;AMM;O009KH;1;2;3
33290;716G67G62;1345;0IPI;1;5;3
129372;716G67G62;2SSYA;OKLOP;0;2;7
76938;716G67G62;AMM;O009KH;4;2;4
39384;716G67G62;AMM;AU7UY;2;4;2
33290;716G67G62;46JY12T;112JGH1;1;2;3
"""


def test_right_hand_sub_grouping_user_example(make_csv, tmp_path) -> None:
    # Key = columns 2+3; only (AMM, O009KH) is duplicated (8 rows).  Those
    # rows sub-group by the cells right of column 3: "1;2;3" (x3), then
    # "3;3;8" (x2), then the rows whose right-hand cells are unique.
    path = make_csv(USER_EXAMPLE, name="example.csv")
    options = CsvOptions(separator=";", has_header=False)
    with (
        CsvRowStream(path, options) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        summary = _scan(
            stream, _dup_rule(ColumnRef("index", 2), ColumnRef("index", 3)), store=store
        )
        assert summary.counts == {"dup": 8}
        matches = store.get_page("dup", 0, 20)
    displayed = [(m.sub_key and m.sub_key.replace("\x1f", ";"), m.row_number) for m in matches]
    assert displayed == [
        ("1;2;3", 3),
        ("1;2;3", 8),
        ("1;2;3", 15),
        ("3;3;8", 6),
        ("3;3;8", 13),
        (None, 1),  # 1;4;3 - unique right-hand cells, sorted last
        (None, 10),  # 1;2;2
        (None, 18),  # 4;2;4
    ]


def test_sub_groups_computed_within_each_key_group(make_csv, tmp_path) -> None:
    # The same right-hand content under two different keys must not merge.
    path = make_csv("id,code,val\n1,A,x\n2,A,x\n3,B,x\n4,B,x\n5,A,y\n")
    with (
        CsvRowStream(path, CsvOptions()) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        _scan(stream, _dup_rule(ColumnRef("header", "code")), store=store)
        matches = store.get_page("dup", 0, 10)
    assert [(m.group_key, m.sub_key, m.row_number) for m in matches] == [
        ("A", "x", 2),
        ("A", "x", 3),
        ("A", None, 6),  # val=y unique within key A
        ("B", "x", 4),
        ("B", "x", 5),
    ]


def test_export_includes_group_column(make_csv, tmp_path) -> None:
    import csv as csv_module

    from lsa.core.report import MatchRowSource, export_rule_matches

    path = make_csv(USER_EXAMPLE, name="example2.csv")
    options = CsvOptions(separator=";", has_header=False)
    with (
        CsvRowStream(path, options) as stream,
        ResultStore(tmp_path / "r.sqlite3") as store,
    ):
        _scan(stream, _dup_rule(ColumnRef("index", 2), ColumnRef("index", 3)), store=store)
        out = tmp_path / "dup.csv"
        with MatchRowSource(path, options) as source:
            export_rule_matches(
                store,
                "dup",
                source,
                out,
                header=None,
                width=stream.width,
                include_group=True,
                unique_label="unique",
            )
    with open(out, encoding="utf-8", newline="") as f:
        rows = list(csv_module.reader(f))
    assert rows[0][:2] == ["row_number", "group"]
    assert [r[1] for r in rows[1:]] == ["1;2;3"] * 3 + ["3;3;8"] * 2 + ["unique"] * 3
    assert rows[1][2] == "92837"  # full original row follows the group column


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
