"""GUI presenters: file selection, import options, rule drafts, report paging."""

import json

from lsa.core.csv_stream import CsvOptions
from lsa.core.i18n import Translator
from lsa.core.rows import FileFormat
from lsa.core.rules import Settings, load_rules
from lsa.gui.presenters import (
    ConditionDraft,
    FileStepPresenter,
    ImportPresenter,
    ReportPresenter,
    RuleDraft,
    RulesPresenter,
    WizardState,
    build_ruleset,
    empty_tokens_to_text,
    format_size,
    parse_empty_tokens,
    ruleset_to_drafts,
)
from lsa.gui.worker import ScanController

TR = Translator("en")


def _draft_rule() -> RuleDraft:
    return RuleDraft(
        id="no-email",
        label="Missing e-mail",
        match="all",
        conditions=[
            ConditionDraft(type="not_empty", column_by="header", column_value="name"),
            ConditionDraft(type="is_empty", column_by="header", column_value="email"),
        ],
    )


# ----------------------------------------------------------------- file step


def test_select_csv_file_prefills_options(make_csv) -> None:
    state = WizardState()
    presenter = FileStepPresenter(state, TR)
    path = make_csv("a;b\n1;2\n" * 40)
    assert presenter.select_file(str(path)) is None
    assert state.file_format is FileFormat.CSV
    assert state.csv_options is not None
    assert state.csv_options.separator == ";"
    assert state.sheets == []
    assert "data.csv" in presenter.file_info()


def test_select_xlsx_file_lists_sheets(make_xlsx) -> None:
    state = WizardState()
    presenter = FileStepPresenter(state, TR)
    path = make_xlsx({"One": [["a"]], "Two": [["b"]]})
    assert presenter.select_file(str(path)) is None
    assert state.file_format is FileFormat.XLSX
    assert state.sheets == ["One", "Two"]
    assert state.sheet == "One"


def test_select_missing_or_unsupported_file(tmp_path) -> None:
    state = WizardState()
    presenter = FileStepPresenter(state, TR)
    assert "cannot be used" in presenter.select_file(str(tmp_path / "nope.csv"))
    bad = tmp_path / "data.parquet"
    bad.write_text("x", encoding="utf-8")
    assert "cannot be used" in presenter.select_file(str(bad))
    assert state.file is None


def test_format_size() -> None:
    assert format_size(512) == "512 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"
    assert format_size(3 * 1024**3) == "3.0 GB"


# ----------------------------------------------------------------- import step


def _csv_state(make_csv, text: str = "name,email\nAna,\nBo,b@c.d\n") -> WizardState:
    state = WizardState()
    path = make_csv(text)
    assert FileStepPresenter(state, TR).select_file(str(path)) is None
    return state


def test_apply_csv_options_updates_state(make_csv) -> None:
    state = _csv_state(make_csv)
    presenter = ImportPresenter(state, TR)
    assert presenter.is_csv
    error = presenter.apply_csv_options(
        separator=";",
        quotechar='"',
        encoding="utf-8",
        has_header=False,
        empty_tokens_text="NA, -",
    )
    assert error is None
    assert state.csv_options == CsvOptions(";", '"', "utf-8", has_header=False)
    assert state.settings.empty_tokens == ("", "NA", "-")
    assert state.has_header is False


def test_apply_csv_options_rejects_bad_input(make_csv) -> None:
    state = _csv_state(make_csv)
    presenter = ImportPresenter(state, TR)
    common = {"quotechar": '"', "has_header": True, "empty_tokens_text": ""}
    assert "separator" in presenter.apply_csv_options(separator="||", encoding="utf-8", **common)
    assert "encoding" in presenter.apply_csv_options(
        separator=",", encoding="not-a-codec", **common
    )


def test_csv_preview_includes_header_and_errors(make_csv) -> None:
    state = _csv_state(make_csv)
    presenter = ImportPresenter(state, TR)
    rows, error = presenter.csv_preview()
    assert error is None
    assert rows[0] == ["name", "email"]
    assert rows[1] == ["Ana", ""]
    # broken options produce an error text, not an exception
    rows, error = presenter.csv_preview(CsvOptions(separator="||"))
    assert rows == []
    assert "Cannot read the file" in error


def test_empty_token_text_round_trip() -> None:
    assert parse_empty_tokens("NA, -,NA") == ("", "NA", "-")
    assert parse_empty_tokens("") == ("",)
    assert empty_tokens_to_text(("", "NA", "-")) == "NA, -"


def test_sheet_and_header_setters(make_xlsx) -> None:
    state = WizardState()
    FileStepPresenter(state, TR).select_file(str(make_xlsx({"One": [["a"]], "Two": [["b"]]})))
    presenter = ImportPresenter(state, TR)
    assert not presenter.is_csv
    presenter.set_sheet("Two")
    presenter.set_has_header(False)
    assert state.sheet == "Two"
    assert state.has_header is False


# ----------------------------------------------------------------- rules step


def test_rule_draft_crud() -> None:
    state = WizardState()
    presenter = RulesPresenter(state, TR)
    first = presenter.add_rule()
    second = presenter.add_rule()
    assert [d.id for d in presenter.drafts] == ["rule-1", "rule-2"]
    assert len(first.conditions) == 1
    presenter.add_condition(0)
    assert len(first.conditions) == 2
    presenter.remove_condition(0, 0)
    assert len(first.conditions) == 1
    presenter.remove_condition(0, 0)  # last condition is kept
    assert len(first.conditions) == 1
    presenter.remove_rule(0)
    assert presenter.drafts == [second]


def test_add_rule_avoids_id_collision() -> None:
    state = WizardState(rule_drafts=[RuleDraft(id="rule-1")])
    presenter = RulesPresenter(state, TR)
    assert presenter.add_rule().id == "rule-2"


def test_validate_requires_rules_and_reports_errors() -> None:
    state = WizardState()
    presenter = RulesPresenter(state, TR)
    ruleset, error = presenter.validate()
    assert ruleset is None
    assert "at least one rule" in error

    state.rule_drafts = [RuleDraft(id="r", conditions=[ConditionDraft(column_value="")])]
    ruleset, error = presenter.validate()
    assert ruleset is None
    assert "Invalid rules" in error


def test_drafts_round_trip_through_ruleset() -> None:
    settings = Settings(empty_tokens=("", "NA"))
    drafts = [
        _draft_rule(),
        RuleDraft(
            id="pair",
            label="pair",
            match="any",
            conditions=[
                ConditionDraft(
                    type="equals_column",
                    column_by="letter",
                    column_value="D",
                    other_by="index",
                    other_value="7",
                )
            ],
        ),
    ]
    ruleset = build_ruleset(drafts, settings)
    assert ruleset.rules[1].conditions[0].other_column.value == 7
    back = ruleset_to_drafts(ruleset)
    assert build_ruleset(back, settings) == ruleset


def test_rules_load_and_save(tmp_path) -> None:
    state = WizardState(rule_drafts=[_draft_rule()], settings=Settings(empty_tokens=("", "NA")))
    presenter = RulesPresenter(state, TR)
    out = tmp_path / "rules.json"
    assert presenter.save(out) is None
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["rules"][0]["id"] == "no-email"

    fresh_state = WizardState()
    fresh = RulesPresenter(fresh_state, TR)
    assert fresh.load(out) is None
    assert fresh_state.settings.empty_tokens == ("", "NA")
    assert [d.id for d in fresh_state.rule_drafts] == ["no-email"]
    assert build_ruleset(fresh_state.rule_drafts, fresh_state.settings) == load_rules(out)


def test_rules_load_reports_errors(tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"version": 1, "rules": []}', encoding="utf-8")
    presenter = RulesPresenter(WizardState(), TR)
    assert "Invalid rules" in presenter.load(bad)


# ----------------------------------------------------------------- worker + report


def _scan_controller(make_csv) -> tuple[ScanController, WizardState]:
    state = WizardState()
    path = make_csv("name,email\n" + "Ana,\n" * 20 + "Bo,b@c.d\n")
    FileStepPresenter(state, TR).select_file(str(path))
    ImportPresenter(state, TR).set_has_header(True)
    state.csv_options = CsvOptions(has_header=True)
    state.rule_drafts = [_draft_rule()]
    return ScanController(), state


def test_scan_controller_end_to_end(make_csv) -> None:
    controller, state = _scan_controller(make_csv)
    ruleset = build_ruleset(state.rule_drafts, state.settings)
    controller.start(
        path=state.file,
        ruleset=ruleset,
        csv_options=state.csv_options,
        sheet=None,
        has_header=True,
    )
    controller.join(timeout=10)
    messages = controller.drain()
    kinds = [m.kind for m in messages]
    assert kinds[-1] == "done"
    assert "error" not in kinds
    assert controller.summary.counts == {"no-email": 20}
    assert controller.store is not None
    assert controller.header == ["name", "email"]

    presenter = ReportPresenter(
        store=controller.store,
        ruleset=ruleset,
        summary=controller.summary,
        file=state.file,
        sheet=None,
        header=controller.header,
        width=controller.width,
        csv_options=state.csv_options,
        tr=TR,
        sample_size=3,
    )
    try:
        assert presenter.match_count("no-email") == 20
        assert presenter.columns() == ["name", "email"]
        first_page = presenter.page_rows("no-email")
        assert first_page[0] == ["2", "Ana", ""]
        assert len(first_page) == 3
        assert presenter.position_text("no-email") == "1-3 of 20"
        presenter.next_page("no-email")
        assert presenter.page_rows("no-email")[0][0] == "5"
        presenter.set_sample_size(10)
        assert len(presenter.page_rows("no-email")) == 10
        assert not presenter.partial
        assert "21" in presenter.summary_text() or "20" in presenter.summary_text()
    finally:
        presenter.close()
        controller.discard_results()


def test_scan_controller_results_valid_for_detects_config_change(make_csv) -> None:
    controller, state = _scan_controller(make_csv)
    ruleset = build_ruleset(state.rule_drafts, state.settings)
    config = {
        "path": state.file,
        "ruleset": ruleset,
        "csv_options": state.csv_options,
        "sheet": None,
        "has_header": True,
    }
    controller.start(**config)
    controller.join(timeout=10)
    controller.drain()
    try:
        assert controller.results_valid_for(**config)
        # equal-by-value config (rebuilt ruleset) still matches
        assert controller.results_valid_for(
            **{**config, "ruleset": build_ruleset(state.rule_drafts, state.settings)}
        )
        # a changed file or rules invalidates the stored results
        assert not controller.results_valid_for(**{**config, "path": state.file.parent / "b.csv"})
        other_rules = build_ruleset([_draft_rule()], Settings(empty_tokens=("", "NA")))
        assert not controller.results_valid_for(**{**config, "ruleset": other_rules})
    finally:
        controller.discard_results()
    assert not controller.results_valid_for(**config)  # discarded


def test_scan_controller_survives_base_exception(monkeypatch, tmp_path) -> None:
    # python-calamine panics are BaseException; the worker must still post an
    # error message instead of dying silently and hanging the GUI.
    class FakePanic(BaseException):
        pass

    def explode(*_args, **_kwargs):
        raise FakePanic("rust panic")

    monkeypatch.setattr("lsa.gui.worker.open_stream", explode)
    controller = ScanController()
    controller.start(
        path=tmp_path / "x.csv",
        ruleset=build_ruleset([_draft_rule()], Settings()),
        csv_options=None,
        sheet=None,
        has_header=True,
    )
    controller.join(timeout=10)
    messages = controller.drain()
    assert messages[-1].kind == "error"
    assert "rust panic" in str(messages[-1].payload)
    assert controller.store is None


def test_scan_controller_reports_errors(tmp_path) -> None:
    controller = ScanController()
    ruleset = build_ruleset([_draft_rule()], Settings())
    controller.start(
        path=tmp_path / "missing.csv",
        ruleset=ruleset,
        csv_options=CsvOptions(),
        sheet=None,
        has_header=True,
    )
    controller.join(timeout=10)
    messages = controller.drain()
    assert messages[-1].kind == "error"
    assert controller.store is None


def test_report_presenter_exports(make_csv, tmp_path) -> None:
    controller, state = _scan_controller(make_csv)
    ruleset = build_ruleset(state.rule_drafts, state.settings)
    controller.start(
        path=state.file,
        ruleset=ruleset,
        csv_options=state.csv_options,
        sheet=None,
        has_header=True,
    )
    controller.join(timeout=10)
    controller.drain()
    presenter = ReportPresenter(
        store=controller.store,
        ruleset=ruleset,
        summary=controller.summary,
        file=state.file,
        sheet=None,
        header=controller.header,
        width=controller.width,
        csv_options=state.csv_options,
        tr=TR,
    )
    try:
        one = tmp_path / "one.csv"
        assert str(one) in presenter.export_rule("no-email", one)
        assert one.read_text(encoding="utf-8").startswith("row_number,name,email")
        out_dir = tmp_path / "all"
        presenter.export_all(out_dir)
        assert (out_dir / "no-email.csv").is_file()
        report_path = tmp_path / "report.json"
        presenter.save_report_json(report_path)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        assert payload["rules"][0]["match_count"] == 20
    finally:
        presenter.close()
        controller.discard_results()
