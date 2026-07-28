"""Duplicate-file extraction: Case A (lowest ID survives), Case B (reference lookup)."""

import csv as csv_module

import pytest

from lsa.core.columns import ColumnRef
from lsa.core.csv_stream import CsvOptions, CsvRowStream
from lsa.core.extract import ColumnMapping, extract_duplicates
from lsa.core.rules import Condition, Rule, RuleSet, Settings
from lsa.core.scan import run_scan
from lsa.core.store import ResultStore

SOURCE = "id;key;val\n9;A;x\n25855941;A;x\n3;A;x\n7;A;q\n5;B;z\n6;B;z\n1;C;u\n"
OPTIONS = CsvOptions(separator=";")

RULE = Rule(
    id="dup",
    label="dup",
    match="all",
    conditions=(Condition("is_duplicate", columns=(ColumnRef("header", "key"),)),),
)


def _prepare(make_csv, tmp_path, settings: Settings | None = None):
    source = make_csv(SOURCE, name="source.csv")
    db = tmp_path / "scan.sqlite3"
    with (
        CsvRowStream(source, OPTIONS) as stream,
        ResultStore(db) as store,
    ):
        run_scan(stream, RuleSet(rules=(RULE,), settings=settings or Settings()), store)
        header, width = stream.header, stream.width
    return source, db, header, width


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv_module.reader(f, delimiter=";"))


def _extract(source, db, header, width, tmp_path, reference, **kwargs):
    out = tmp_path / "duplicates.csv"
    defaults = {
        "scan_db_path": db,
        "rule_id": "dup",
        "source_path": source,
        "source_csv_options": OPTIONS,
        "source_header": header,
        "source_width": width,
        "reference_path": reference,
        "reference_csv_options": CsvOptions(separator=";"),
        "reference_sheet": None,
        "reference_has_header": True,
        "mapping": ColumnMapping(pairs=((0, 0), (1, 1), (2, 2))),
        "settings": Settings(),
        "id_column": 0,
        "out_path": out,
        "out_separator": ";",
    }
    defaults.update(kwargs)
    stats = extract_duplicates(**defaults)
    return stats, _read(out)


def test_case_a_keeps_lowest_numeric_id(make_csv, tmp_path) -> None:
    source, db, header, width = _prepare(make_csv, tmp_path)
    reference = make_csv("id;key;val\n7;A;q\n", name="ref.csv")
    stats, rows = _extract(source, db, header, width, tmp_path, reference)

    assert rows[0] == ["id", "key", "val"]
    written_ids = [r[0] for r in rows[1:]]
    # A/x sub-group: ids 9, 25855941, 3 -> 3 survives (numeric, not lexicographic)
    # B/z sub-group: ids 5, 6 -> 5 survives
    assert sorted(written_ids, key=int) == ["6", "9", "25855941"]
    assert stats.case_a_rows == 3
    assert stats.case_b_rows == 0  # row 7 exists in the reference -> kept
    assert stats.kept_rows == 3  # survivors 3 and 5, plus reference-backed 7
    assert stats.groups_processed == 2  # keys A and B (C is not duplicated)
    assert stats.reference_rows == 1
    assert stats.duplicates_written == 3
    assert not stats.cancelled


def test_case_b_writes_rows_missing_from_reference(make_csv, tmp_path) -> None:
    source, db, header, width = _prepare(make_csv, tmp_path)
    reference = make_csv("id;key;val\n999;ZZZ;none\n", name="ref.csv")
    stats, rows = _extract(source, db, header, width, tmp_path, reference)
    written_ids = [r[0] for r in rows[1:]]
    assert "7" in written_ids  # unique row absent from the reference
    assert stats.case_b_rows == 1
    assert stats.kept_rows == 2  # only the two sub-group survivors


def test_case_b_uses_mapped_columns_only(make_csv, tmp_path) -> None:
    # Reference has different column order and an extra column; only
    # key and val are mapped - the id is ignored in the comparison.
    source, db, header, width = _prepare(make_csv, tmp_path)
    reference = make_csv("extra;value;code\nfoo;q;A\n", name="ref.csv")
    stats, _rows = _extract(
        source,
        db,
        header,
        width,
        tmp_path,
        reference,
        mapping=ColumnMapping(pairs=((1, 2), (2, 1))),  # key->code, val->value
    )
    assert stats.case_b_rows == 0  # (A, q) found in the reference
    assert stats.kept_rows == 3


def test_reference_comparison_respects_case_setting(make_csv, tmp_path) -> None:
    settings = Settings(case_sensitive=False)
    source, db, header, width = _prepare(make_csv, tmp_path, settings=settings)
    reference = make_csv("id;key;val\n7;a; Q \n", name="ref.csv")
    stats, _rows = _extract(source, db, header, width, tmp_path, reference, settings=settings)
    assert stats.case_b_rows == 0  # "a"/" Q " matches "A"/"q" when folded+trimmed


def test_non_numeric_ids_fall_back_to_text_order(make_csv, tmp_path) -> None:
    source = make_csv("id;key;val\nB7;A;x\nA9;A;x\n", name="source.csv")
    db = tmp_path / "scan.sqlite3"
    with CsvRowStream(source, OPTIONS) as stream, ResultStore(db) as store:
        run_scan(stream, RuleSet(rules=(RULE,)), store)
        header, width = stream.header, stream.width
    reference = make_csv("id;key;val\n", name="ref.csv")
    stats, rows = _extract(source, db, header, width, tmp_path, reference)
    assert [r[0] for r in rows[1:]] == ["B7"]  # "A9" < "B7" as text -> A9 kept
    assert stats.kept_rows == 1


def test_nan_ids_do_not_poison_survivor_selection(make_csv, tmp_path) -> None:
    # float("nan") compares false with everything and would silently win/lose
    # min-comparisons; NaN ids must fall back to text ordering instead.
    source = make_csv("id;key;val\nnan;A;x\n5;A;x\n3;A;x\n", name="source.csv")
    db = tmp_path / "scan.sqlite3"
    with CsvRowStream(source, OPTIONS) as stream, ResultStore(db) as store:
        run_scan(stream, RuleSet(rules=(RULE,)), store)
        header, width = stream.header, stream.width
    reference = make_csv("id;key;val\n", name="ref.csv")
    stats, rows = _extract(source, db, header, width, tmp_path, reference)
    assert sorted(r[0] for r in rows[1:]) == ["5", "nan"]  # numeric 3 survives
    assert stats.kept_rows == 1


def test_empty_mapping_rejected(make_csv, tmp_path) -> None:
    source, db, header, width = _prepare(make_csv, tmp_path)
    reference = make_csv("id;key;val\n", name="ref.csv")
    with pytest.raises(ValueError, match="at least one column"):
        _extract(source, db, header, width, tmp_path, reference, mapping=ColumnMapping(pairs=()))


def test_extract_from_xlsx_source_uses_cached_cells(make_xlsx, tmp_path, make_csv) -> None:
    from lsa.core.excel_stream import OpenpyxlRowStream

    source = make_xlsx({"Data": [["id", "key", "val"], [4, "A", "x"], [2, "A", "x"]]})
    db = tmp_path / "scan.sqlite3"
    stream = OpenpyxlRowStream(source)
    with ResultStore(db) as store:
        try:
            run_scan(stream, RuleSet(rules=(RULE,)), store)
            header, width = stream.header, stream.width
        finally:
            stream.close()
    reference = make_csv("id;key;val\n", name="ref.csv")
    stats, rows = _extract(source, db, header, width, tmp_path, reference, source_csv_options=None)
    assert [r[0] for r in rows[1:]] == ["4"]  # id 2 survives
    assert stats.case_a_rows == 1 and stats.kept_rows == 1
