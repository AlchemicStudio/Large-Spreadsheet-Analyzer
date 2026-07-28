"""Duplicate-file verification: conditions A/B, invalid rows, viewer queries."""

from lsa.core.columns import ColumnRef
from lsa.core.csv_stream import CsvOptions, CsvRowStream
from lsa.core.extract import ColumnMapping, extract_duplicates
from lsa.core.i18n import Translator
from lsa.core.rules import Condition, Rule, RuleSet, Settings
from lsa.core.scan import run_scan
from lsa.core.store import ResultStore
from lsa.core.verify import VerificationStore, verify_duplicates

SOURCE = "id;key;val\n9;A;x\n25855941;A;x\n3;A;x\n7;A;q\n5;B;z\n6;B;z\n1;C;u\n"
OPTIONS = CsvOptions(separator=";")
RULESET = RuleSet(
    rules=(
        Rule(
            id="dup",
            label="dup",
            match="all",
            conditions=(Condition("is_duplicate", columns=(ColumnRef("header", "key"),)),),
        ),
    )
)
MAPPING = ColumnMapping(pairs=((0, 0), (1, 1), (2, 2)))


def _pipeline(make_csv, tmp_path, *, reference_text="id;key;val\n7;A;q\n"):
    """Scan + extract, keeping every artifact verification needs."""
    source = make_csv(SOURCE, name="source.csv")
    reference = make_csv(reference_text, name="ref.csv")
    scan_db = tmp_path / "scan.sqlite3"
    with CsvRowStream(source, OPTIONS) as stream, ResultStore(scan_db) as store:
        run_scan(stream, RULESET, store)
        header, width = stream.header, stream.width
    dup_file = tmp_path / "duplicates.csv"
    ref_db = tmp_path / "refkeys.sqlite3"
    ref_db.touch()
    extract_duplicates(
        scan_db_path=scan_db,
        rule_id="dup",
        source_path=source,
        source_csv_options=OPTIONS,
        source_header=header,
        source_width=width,
        reference_path=reference,
        reference_csv_options=CsvOptions(separator=";"),
        reference_sheet=None,
        reference_has_header=True,
        mapping=MAPPING,
        settings=Settings(),
        id_column=0,
        out_path=dup_file,
        out_separator=";",
        ref_db_path=ref_db,  # parent-owned: must survive for verification
    )
    assert ref_db.stat().st_size > 0  # retained, not deleted by the extraction
    return {
        "scan_db_path": scan_db,
        "ruleset": RULESET,
        "rule_id": "dup",
        "source_path": source,
        "source_csv_options": OPTIONS,
        "source_header": header,
        "source_width": width,
        "dup_file_path": dup_file,
        "dup_separator": ";",
        "mapping": MAPPING,
        "settings": Settings(),
        "ref_db_path": ref_db,
    }


def test_clean_extraction_verifies_all_valid(make_csv, tmp_path) -> None:
    kwargs = _pipeline(make_csv, tmp_path)
    out_db = tmp_path / "verify.sqlite3"
    stats = verify_duplicates(out_db_path=out_db, **kwargs)
    # duplicate file: 9, 25855941 (A/x sub-group) and 6 (B/z) - all condition A
    assert stats.total == 3
    assert stats.valid_a == 3
    assert stats.valid_b == 0
    assert stats.invalid == 0
    with VerificationStore(out_db, read_only=True) as store:
        rows = store.page(0, 10)
        assert [r.condition for r in rows] == ["A", "A", "A"]
        assert all(r.valid for r in rows)
        # condition A lists the original row indices with their first cells
        a_row = next(r for r in rows if r.first_cell == "9")
        assert a_row.occurrences == 3
        assert a_row.matches_display == "2:9, 3:25855941, 4:3"


def test_condition_b_and_invalid_rows(make_csv, tmp_path) -> None:
    # reference misses (A,q): row 7 is extracted as Case B -> condition B
    kwargs = _pipeline(make_csv, tmp_path, reference_text="id;key;val\n999;Z;zz\n")
    dup_file = kwargs["dup_file_path"]
    with open(dup_file, "a", encoding="utf-8", newline="") as f:
        f.write("999;NEW;new\n")  # tampered: not in the original at all
    out_db = tmp_path / "verify.sqlite3"
    stats = verify_duplicates(out_db_path=out_db, **kwargs)
    assert stats.total == 5  # 3 case A + row 7 (B) + tampered
    assert stats.valid_a == 3
    assert stats.valid_b == 1
    assert stats.invalid == 1
    with VerificationStore(out_db, read_only=True) as store:
        by_first = {r.first_cell: r for r in store.page(0, 10)}
        assert by_first["7"].condition == "B"
        assert by_first["7"].occurrences == 1
        assert by_first["999"].valid is False
        assert by_first["999"].occurrences == 0


def test_unique_row_present_in_reference_is_invalid(make_csv, tmp_path) -> None:
    kwargs = _pipeline(make_csv, tmp_path)  # reference contains (7, A, q)
    dup_file = kwargs["dup_file_path"]
    with open(dup_file, "a", encoding="utf-8", newline="") as f:
        f.write("7;A;q\n")  # should have been kept: unique AND in reference
    out_db = tmp_path / "verify.sqlite3"
    stats = verify_duplicates(out_db_path=out_db, **kwargs)
    assert stats.invalid == 1
    with VerificationStore(out_db, read_only=True) as store:
        bad = next(r for r in store.page(0, 10) if r.first_cell == "7")
        assert bad.valid is False and bad.condition == ""


def _mini_pipeline(make_csv, tmp_path, source_text: str, *, reference_text: str):
    """Scan + extract an arbitrary source; returns verify kwargs."""
    source = make_csv(source_text, name="source.csv")
    reference = make_csv(reference_text, name="ref.csv")
    scan_db = tmp_path / "scan.sqlite3"
    with CsvRowStream(source, OPTIONS) as stream, ResultStore(scan_db) as store:
        run_scan(stream, RULESET, store)
        header, width = stream.header, stream.width
    dup_file = tmp_path / "duplicates.csv"
    ref_db = tmp_path / "refkeys.sqlite3"
    ref_db.touch()
    extract_duplicates(
        scan_db_path=scan_db,
        rule_id="dup",
        source_path=source,
        source_csv_options=OPTIONS,
        source_header=header,
        source_width=width,
        reference_path=reference,
        reference_csv_options=CsvOptions(separator=";"),
        reference_sheet=None,
        reference_has_header=True,
        mapping=ColumnMapping(pairs=tuple((i, i) for i in range(width))),
        settings=Settings(),
        id_column=0,
        out_path=dup_file,
        out_separator=";",
        ref_db_path=ref_db,
    )
    return {
        "scan_db_path": scan_db,
        "ruleset": RULESET,
        "rule_id": "dup",
        "source_path": source,
        "source_csv_options": OPTIONS,
        "source_header": header,
        "source_width": width,
        "dup_file_path": dup_file,
        "dup_separator": ";",
        "mapping": ColumnMapping(pairs=tuple((i, i) for i in range(width))),
        "settings": Settings(),
        "ref_db_path": ref_db,
    }


def test_ragged_source_rows_verify_valid(make_csv, tmp_path) -> None:
    # Ragged rows are padded when written to the duplicate file; the right
    # key must be padding-insensitive or correct extractions audit INVALID.
    kwargs = _mini_pipeline(
        make_csv,
        tmp_path,
        "id;key;c;d\n1;A\n2;A\n3;B;x;y\n",
        reference_text="id;key;c;d\n",
    )
    out_db = tmp_path / "verify.sqlite3"
    stats = verify_duplicates(out_db_path=out_db, **kwargs)
    assert stats.total == 1
    assert stats.valid_a == 1
    assert stats.invalid == 0


def test_trailing_empty_duplicate_rows_are_audited(make_csv, tmp_path) -> None:
    # Blank source rows share the empty key; the extracted (fully empty,
    # padded) row is the dup file's last record and must still be verified.
    kwargs = _mini_pipeline(
        make_csv,
        tmp_path,
        "id;key;val\n;;\n;;\n1;A;x\n",
        reference_text="id;key;val\n",
    )
    out_db = tmp_path / "verify.sqlite3"
    stats = verify_duplicates(out_db_path=out_db, **kwargs)
    assert stats.total == 1  # the extracted blank-row duplicate is audited
    assert stats.valid_a == 1
    assert stats.invalid == 0
    assert stats.malformed_rows == 0


def test_store_filter_pagination_and_position(make_csv, tmp_path) -> None:
    kwargs = _pipeline(make_csv, tmp_path)
    out_db = tmp_path / "verify.sqlite3"
    verify_duplicates(out_db_path=out_db, **kwargs)
    with VerificationStore(out_db, read_only=True) as store:
        assert store.count() == 3
        assert [r.seq for r in store.page(0, 2)] == [1, 2]
        assert [r.seq for r in store.page(2, 2)] == [3]
        # filter on the first two cells only
        assert store.count("2585") == 1
        assert store.page(0, 10, "2585")[0].first_cell == "25855941"
        assert store.count("x") == 0  # 'x' only appears in the third column
        assert store.position_of(3) == 2


def test_verify_presenter_paging_filter_and_goto(make_csv, tmp_path) -> None:
    from lsa.gui.presenters import VerifyPresenter

    kwargs = _pipeline(make_csv, tmp_path)
    out_db = tmp_path / "verify.sqlite3"
    stats = verify_duplicates(out_db_path=out_db, **kwargs)
    presenter = VerifyPresenter(
        out_db,
        source_header=kwargs["source_header"],
        source_width=kwargs["source_width"],
        tr=Translator("en"),
        page_size=2,
    )
    try:
        assert presenter.total() == 3
        columns = presenter.table_columns()
        assert columns[:5] == [
            "#",
            "Status",
            "Condition",
            "Occurrences",
            "Original rows (row:first cell)",
        ]
        assert columns[5:] == ["id", "key", "val"]
        first_page = presenter.page_rows()
        assert len(first_page) == 2
        assert first_page[0][1] == "VALID"
        assert presenter.position_text() == "1-2 of 3"
        presenter.next_page()
        assert len(presenter.page_rows()) == 1
        assert not presenter.can_next
        presenter.goto_page(1)
        assert presenter.offset == 0
        presenter.goto_row(3)
        assert presenter.offset == 2
        presenter.set_filter("6")
        assert presenter.total() == 1
        assert presenter.page_rows()[0][5] == "6"
        presenter.set_filter("")
        presenter.set_page_size(1)
        presenter.goto_row(2)
        assert presenter.offset == 1
        assert "3" in presenter.summary_text(stats)
    finally:
        presenter.close()
