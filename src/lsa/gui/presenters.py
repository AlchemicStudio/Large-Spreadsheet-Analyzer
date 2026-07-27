"""Presenter / view-model layer for the wizard.

Everything here is plain Python — no tkinter imports — so the GUI logic is
fully unit-testable.  The Tk views (``lsa.gui.views``) stay thin: they render
presenter state and forward user actions back to these classes.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field, replace
from itertools import islice
from pathlib import Path
from typing import Any

from lsa.core.columns import index_to_letter
from lsa.core.csv_stream import CsvOptions, CsvRowStream, detect_csv_options
from lsa.core.excel_stream import list_sheets, needs_memory_warning
from lsa.core.i18n import Translator
from lsa.core.report import (
    MatchPager,
    MatchRowSource,
    build_report,
    export_all_matches,
    export_rule_matches,
    format_sub_key,
    rule_has_groups,
    save_report,
)
from lsa.core.rows import FileFormat, UnsupportedFormatError, detect_format
from lsa.core.rules import (
    RULES_FILE_VERSION,
    RuleSet,
    RuleValidationError,
    Settings,
    load_rules,
    ruleset_from_dict,
    save_rules,
)
from lsa.core.scan import ScanSummary
from lsa.core.store import ResultStore

PREVIEW_ROW_COUNT = 20


# --------------------------------------------------------------------------- rules drafts


@dataclass
class ConditionDraft:
    """Mutable editing model of one condition (values as raw strings)."""

    type: str = "is_empty"
    column_by: str = "header"
    column_value: str = ""
    other_by: str = "header"
    other_value: str = ""


@dataclass
class RuleDraft:
    """Mutable editing model of one rule."""

    id: str
    label: str = ""
    match: str = "all"
    conditions: list[ConditionDraft] = field(default_factory=lambda: [ConditionDraft()])


def _ref_value(by: str, raw: str) -> Any:
    value = raw.strip()
    if by == "index":
        try:
            return int(value)
        except ValueError:
            return value  # validation will report a proper message
    return value


def drafts_to_document(drafts: list[RuleDraft], settings: Settings) -> dict[str, Any]:
    """Serialize drafts to a rules document (same schema as rules.json)."""
    rules = []
    for draft in drafts:
        conditions: list[dict[str, Any]] = []
        for cond in draft.conditions:
            first_ref = {
                "by": cond.column_by,
                "value": _ref_value(cond.column_by, cond.column_value),
            }
            if cond.type == "is_duplicate":
                columns = [first_ref]
                if cond.other_value.strip():
                    columns.append(
                        {"by": cond.other_by, "value": _ref_value(cond.other_by, cond.other_value)}
                    )
                entry: dict[str, Any] = {"type": cond.type, "columns": columns}
            else:
                entry = {"type": cond.type, "column": first_ref}
                if cond.type == "equals_column":
                    entry["other_column"] = {
                        "by": cond.other_by,
                        "value": _ref_value(cond.other_by, cond.other_value),
                    }
            conditions.append(entry)
        rules.append(
            {
                "id": draft.id.strip(),
                "label": draft.label.strip() or draft.id.strip(),
                "match": draft.match,
                "conditions": conditions,
            }
        )
    return {
        "version": RULES_FILE_VERSION,
        "settings": {
            "trim": settings.trim,
            "case_sensitive": settings.case_sensitive,
            "empty_tokens": list(settings.empty_tokens),
        },
        "rules": rules,
    }


def build_ruleset(drafts: list[RuleDraft], settings: Settings) -> RuleSet:
    """Validate drafts into a RuleSet (raises RuleValidationError)."""
    return ruleset_from_dict(drafts_to_document(drafts, settings))


def ruleset_to_drafts(ruleset: RuleSet) -> list[RuleDraft]:
    """Convert a loaded RuleSet back into editable drafts.

    Raises ValueError when the rules use features the visual editor cannot
    represent (an is_duplicate condition with more than 2 key columns).
    """
    drafts = []
    for rule in ruleset.rules:
        conditions = []
        for cond in rule.conditions:
            if cond.type == "is_duplicate":
                columns = cond.columns or ()
                if len(columns) > 2:
                    raise ValueError(
                        f"rule {rule.id!r}: is_duplicate with more than 2 columns can only "
                        "be edited in the JSON file, not in the visual editor"
                    )
                draft = ConditionDraft(
                    type=cond.type,
                    column_by=columns[0].by,
                    column_value=str(columns[0].value),
                )
                if len(columns) > 1:
                    draft.other_by = columns[1].by
                    draft.other_value = str(columns[1].value)
                conditions.append(draft)
                continue
            draft = ConditionDraft(
                type=cond.type,
                column_by=cond.column.by,
                column_value=str(cond.column.value),
            )
            if cond.other_column is not None:
                draft.other_by = cond.other_column.by
                draft.other_value = str(cond.other_column.value)
            conditions.append(draft)
        drafts.append(
            RuleDraft(id=rule.id, label=rule.label, match=rule.match, conditions=conditions)
        )
    return drafts


# --------------------------------------------------------------------------- wizard state


@dataclass
class WizardState:
    """Everything the wizard collects across its five steps."""

    file: Path | None = None
    file_format: FileFormat | None = None
    csv_options: CsvOptions | None = None
    sheets: list[str] = field(default_factory=list)
    sheet: str | None = None
    has_header: bool = True
    memory_warning: bool = False
    settings: Settings = field(default_factory=Settings)
    rule_drafts: list[RuleDraft] = field(default_factory=list)


class FileStepPresenter:
    """Step 1: choose a file."""

    def __init__(self, state: WizardState, tr: Translator):
        self._state = state
        self._tr = tr

    def select_file(self, path_str: str) -> str | None:
        """Validate and register the chosen file; returns an error text or None."""
        state = self._state
        path = Path(path_str).expanduser()
        if not path.is_file():
            return self._tr("file.invalid", error=path_str)
        try:
            fmt = detect_format(path)
        except UnsupportedFormatError as exc:
            return self._tr("file.invalid", error=str(exc))
        try:
            if fmt is FileFormat.CSV:
                options = detect_csv_options(path)
                sheets: list[str] = []
                sheet = None
                has_header = options.has_header
            else:
                options = None
                sheets = list_sheets(path)
                sheet = sheets[0] if sheets else None
                has_header = True
        except Exception as exc:  # unreadable/corrupt file
            return self._tr("file.invalid", error=str(exc))
        state.file = path
        state.file_format = fmt
        state.csv_options = options
        state.sheets = sheets
        state.sheet = sheet
        state.has_header = has_header
        state.memory_warning = needs_memory_warning(path)
        return None

    def file_info(self) -> str:
        state = self._state
        if state.file is None:
            return self._tr("file.placeholder")
        size = state.file.stat().st_size
        return self._tr("file.info", name=state.file.name, size=format_size(size))


def format_size(size: int) -> str:
    """Human-readable file size (binary units)."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} GB"  # pragma: no cover - loop always returns


class ImportPresenter:
    """Step 2: CSV dialect / sheet picker, with live preview for CSV."""

    def __init__(self, state: WizardState, tr: Translator):
        self._state = state
        self._tr = tr

    @property
    def is_csv(self) -> bool:
        return self._state.file_format is FileFormat.CSV

    def csv_options(self) -> CsvOptions:
        return self._state.csv_options or CsvOptions()

    def apply_csv_options(
        self,
        *,
        separator: str,
        quotechar: str,
        encoding: str,
        has_header: bool,
        empty_tokens_text: str,
    ) -> str | None:
        """Validate and store the dialect; returns an error text or None."""
        if len(separator) != 1:
            return self._tr("csv.preview.error", error=f"separator {separator!r}")
        if len(quotechar) > 1:
            return self._tr("csv.preview.error", error=f"quote character {quotechar!r}")
        try:
            codecs.lookup(encoding)
        except LookupError:
            return self._tr("csv.preview.error", error=f"unknown encoding {encoding!r}")
        state = self._state
        state.csv_options = CsvOptions(
            separator=separator,
            quotechar=quotechar,
            encoding=encoding,
            has_header=has_header,
        )
        state.has_header = has_header
        state.settings = replace(state.settings, empty_tokens=parse_empty_tokens(empty_tokens_text))
        return None

    def csv_preview(
        self, options: CsvOptions | None = None, limit: int = PREVIEW_ROW_COUNT
    ) -> tuple[list[list[str]], str | None]:
        """First rows of the file under the given (or stored) options.

        The header row, when enabled, is included as the first preview row so
        the user can see exactly how the file splits.  Returns (rows, error).
        """
        state = self._state
        if state.file is None:
            return [], None
        options = options or self.csv_options()
        try:
            with CsvRowStream(state.file, options) as stream:
                rows = [] if stream.header is None else [list(stream.header)]
                rows.extend(row.cells for row in islice(stream.rows(), limit))
            return rows, None
        except Exception as exc:
            return [], self._tr("csv.preview.error", error=str(exc))

    def set_sheet(self, sheet: str) -> None:
        self._state.sheet = sheet

    def set_has_header(self, has_header: bool) -> None:
        state = self._state
        state.has_header = has_header
        if state.csv_options is not None:
            state.csv_options = replace(state.csv_options, has_header=has_header)


def parse_empty_tokens(text: str) -> tuple[str, ...]:
    """Parse the comma-separated empty-token list; "" is always included."""
    tokens = [""]
    for raw in text.split(","):
        token = raw.strip()
        if token and token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def empty_tokens_to_text(tokens: tuple[str, ...]) -> str:
    """Inverse of :func:`parse_empty_tokens` for pre-filling the entry."""
    return ", ".join(t for t in tokens if t)


class RulesPresenter:
    """Step 3: visual rule builder plus JSON load/save."""

    def __init__(self, state: WizardState, tr: Translator):
        self._state = state
        self._tr = tr

    @property
    def drafts(self) -> list[RuleDraft]:
        return self._state.rule_drafts

    def add_rule(self) -> RuleDraft:
        existing = {d.id for d in self.drafts}
        n = len(self.drafts) + 1
        while f"rule-{n}" in existing:
            n += 1
        draft = RuleDraft(id=f"rule-{n}")
        self.drafts.append(draft)
        return draft

    def remove_rule(self, index: int) -> None:
        del self.drafts[index]

    def add_condition(self, rule_index: int) -> ConditionDraft:
        cond = ConditionDraft()
        self.drafts[rule_index].conditions.append(cond)
        return cond

    def remove_condition(self, rule_index: int, cond_index: int) -> None:
        conditions = self.drafts[rule_index].conditions
        if len(conditions) > 1:
            del conditions[cond_index]

    def set_trim(self, value: bool) -> None:
        self._state.settings = replace(self._state.settings, trim=value)

    def set_case_sensitive(self, value: bool) -> None:
        self._state.settings = replace(self._state.settings, case_sensitive=value)

    def validate(self) -> tuple[RuleSet | None, str | None]:
        """Build the RuleSet from drafts; returns (ruleset, error_text)."""
        if not self.drafts:
            return None, self._tr("rules.need_one")
        try:
            return build_ruleset(self.drafts, self._state.settings), None
        except RuleValidationError as exc:
            return None, self._tr("rules.invalid", errors="\n".join(exc.errors))

    def load(self, path: str | Path) -> str | None:
        """Load a rules JSON file into the editor; returns an error text or None."""
        try:
            ruleset = load_rules(path)
            drafts = ruleset_to_drafts(ruleset)
        except RuleValidationError as exc:
            return self._tr("rules.invalid", errors="\n".join(exc.errors))
        except ValueError as exc:
            return self._tr("rules.invalid", errors=str(exc))
        self._state.settings = ruleset.settings
        self._state.rule_drafts = drafts
        return None

    def save(self, path: str | Path) -> str | None:
        """Save the current drafts as JSON; returns an error text or None."""
        ruleset, error = self.validate()
        if ruleset is None:
            return error
        save_rules(ruleset, path)
        return None


class ReportPresenter:
    """Step 5: per-rule sections with pagination and exports."""

    def __init__(
        self,
        *,
        store: ResultStore,
        ruleset: RuleSet,
        summary: ScanSummary,
        file: Path,
        sheet: str | None,
        header: list[str] | None,
        width: int,
        csv_options: CsvOptions | None,
        tr: Translator,
        sample_size: int = 3,
    ):
        self._store = store
        self._ruleset = ruleset
        self._summary = summary
        self._file = file
        self._sheet = sheet
        self._header = header
        self._width = width
        self._csv_options = csv_options
        self._tr = tr
        self.sample_size = sample_size
        self._source = MatchRowSource(file, csv_options)
        self._pagers = {rule.id: MatchPager(store, rule.id, sample_size) for rule in ruleset.rules}

    @property
    def partial(self) -> bool:
        return self._summary.cancelled

    def summary_text(self) -> str:
        return self._tr(
            "report.summary",
            rows=f"{self._summary.rows_scanned:,}",
            seconds=f"{self._summary.elapsed_seconds:.1f}",
        )

    def columns(self) -> list[str]:
        """Data column titles: the file header, or letters for headerless files."""
        if self._header is not None:
            return list(self._header)
        return [index_to_letter(i) for i in range(self._width)]

    def rule_ids(self) -> list[str]:
        return [rule.id for rule in self._ruleset.rules]

    def rule_label(self, rule_id: str) -> str:
        for rule in self._ruleset.rules:
            if rule.id == rule_id:
                return rule.label
        return rule_id

    def has_groups(self, rule_id: str) -> bool:
        """Whether this rule's matches carry duplicate-group information."""
        return any(rule_has_groups(rule) for rule in self._ruleset.rules if rule.id == rule_id)

    def table_columns(self, rule_id: str) -> list[str]:
        """Column titles for the report table of one rule."""
        columns = [self._tr("report.row_number")]
        if self.has_groups(rule_id):
            columns.append(self._tr("report.group"))
        return columns + self.columns()

    def match_count(self, rule_id: str) -> int:
        return self._summary.counts.get(rule_id, 0)

    def position_text(self, rule_id: str) -> str:
        pager = self._pagers[rule_id]
        if pager.total == 0:
            return self._tr("report.no_matches")
        first = pager.offset + 1
        last = min(pager.offset + pager.page_size, pager.total)
        return self._tr("report.position", first=first, last=last, total=pager.total)

    def can_prev(self, rule_id: str) -> bool:
        return self._pagers[rule_id].has_prev

    def can_next(self, rule_id: str) -> bool:
        return self._pagers[rule_id].has_next

    def page_rows(self, rule_id: str) -> list[list[str]]:
        """Display rows for the current page: [row_number, (group,) cell, ...].

        For duplicate rules a group column follows the row number: the shared
        right-hand content for grouped rows, or the localized "unique" label.
        """
        width = self._width
        grouped = self.has_groups(rule_id)
        unique_label = self._tr("report.group.unique")
        rows = []
        for match in self._pagers[rule_id].page():
            cells = self._source.cells_for(match)
            padded = list(cells[:width]) + [""] * max(0, width - len(cells))
            row = [str(match.row_number), *padded]
            if grouped:
                row.insert(1, format_sub_key(match, unique_label))
            rows.append(row)
        return rows

    def next_page(self, rule_id: str) -> None:
        self._pagers[rule_id].next()

    def prev_page(self, rule_id: str) -> None:
        self._pagers[rule_id].prev()

    def set_sample_size(self, size: int) -> None:
        self.sample_size = max(1, size)
        for pager in self._pagers.values():
            pager.set_page_size(self.sample_size)

    def export_rule(self, rule_id: str, out_path: str | Path) -> str:
        export_rule_matches(
            self._store,
            rule_id,
            self._source,
            out_path,
            header=self._header,
            width=self._width,
            include_group=self.has_groups(rule_id),
            unique_label=self._tr("report.group.unique"),
        )
        return self._tr("report.export.done", path=out_path)

    def export_all(self, out_dir: str | Path) -> str:
        export_all_matches(
            self._store,
            self._ruleset,
            self._source,
            out_dir,
            header=self._header,
            width=self._width,
            unique_label=self._tr("report.group.unique"),
        )
        return self._tr("report.export.done", path=out_dir)

    def save_report_json(self, out_path: str | Path) -> str:
        report = build_report(
            file=self._file,
            sheet=self._sheet,
            ruleset=self._ruleset,
            summary=self._summary,
            store=self._store,
            sample_size=self.sample_size,
        )
        save_report(report, out_path)
        return self._tr("report.export.done", path=out_path)

    def close(self) -> None:
        self._source.close()
