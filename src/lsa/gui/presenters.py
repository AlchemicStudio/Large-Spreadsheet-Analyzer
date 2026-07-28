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
from lsa.core.extract import ColumnMapping
from lsa.core.i18n import Translator
from lsa.core.report import (
    GroupPager,
    MatchPager,
    MatchRowSource,
    build_report,
    export_all_matches,
    export_rule_matches,
    format_group_key,
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
from lsa.core.streams import open_stream

PREVIEW_ROW_COUNT = 20
GROUPS_PER_PAGE = 5


@dataclass(frozen=True)
class GroupBlock:
    """One rendered block of the report for a duplicate rule."""

    title: str
    rows: list[list[str]]
    more_label: str | None


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


@dataclass
class MappingRow:
    """One line of the column-mapping editor."""

    source_index: int
    source_label: str
    ref_index: int | None


class DupFilePresenter:
    """Step 6: reference file, column mapping, duplicate-file extraction."""

    def __init__(self, state: WizardState, controller, ruleset: RuleSet, tr: Translator):
        self._state = state
        self._controller = controller
        self._ruleset = ruleset
        self._tr = tr
        self.reference_path: Path | None = None
        self.reference_csv_options: CsvOptions | None = None
        self.reference_sheets: list[str] = []
        self.reference_sheet: str | None = None
        self.reference_has_header: bool = True
        self.reference_header: list[str] | None = None
        self.reference_width: int = 0
        self.mapping: list[MappingRow] = []

    def duplicate_rules(self) -> list:
        """The rules this step can extract for (those with is_duplicate)."""
        return [rule for rule in self._ruleset.rules if rule_has_groups(rule)]

    def source_columns(self) -> list[str]:
        header = self._controller.header
        if header is not None:
            return list(header)
        return [index_to_letter(i) for i in range(self._controller.width)]

    def select_reference(self, path_str: str) -> str | None:
        """Register the reference file and build a default mapping."""
        path = Path(path_str).expanduser()
        if not path.is_file():
            return self._tr("file.invalid", error=path_str)
        try:
            fmt = detect_format(path)
        except UnsupportedFormatError as exc:
            return self._tr("file.invalid", error=str(exc))
        try:
            if fmt is FileFormat.CSV:
                self.reference_csv_options = detect_csv_options(path)
                self.reference_has_header = self.reference_csv_options.has_header
                self.reference_sheets = []
                self.reference_sheet = None
            else:
                self.reference_csv_options = None
                self.reference_sheets = list_sheets(path)
                self.reference_sheet = self.reference_sheets[0] if self.reference_sheets else None
                self.reference_has_header = True
            self.reference_path = path
            self._peek_reference()
        except Exception as exc:
            self.reference_path = None
            return self._tr("file.invalid", error=str(exc))
        self.reset_mapping()
        return None

    def _peek_reference(self) -> None:
        assert self.reference_path is not None
        options = self.reference_csv_options
        if options is not None and options.has_header != self.reference_has_header:
            options = replace(options, has_header=self.reference_has_header)
            self.reference_csv_options = options
        stream = open_stream(
            self.reference_path,
            csv_options=options,
            sheet=self.reference_sheet,
            has_header=self.reference_has_header,
        )
        try:
            self.reference_header = stream.header
            self.reference_width = stream.width
        finally:
            stream.close()

    def set_reference_sheet(self, sheet: str) -> str | None:
        self.reference_sheet = sheet
        return self._repeek()

    def set_reference_has_header(self, has_header: bool) -> str | None:
        self.reference_has_header = has_header
        return self._repeek()

    def _repeek(self) -> str | None:
        if self.reference_path is None:
            return None
        try:
            self._peek_reference()
        except Exception as exc:
            return self._tr("file.invalid", error=str(exc))
        self.reset_mapping()
        return None

    def reference_column_labels(self) -> list[str]:
        if self.reference_header is not None:
            return list(self.reference_header)
        return [index_to_letter(i) for i in range(self.reference_width)]

    def reset_mapping(self) -> None:
        """Default mapping: same header name first, then same position."""
        source = self.source_columns()
        ref_labels = self.reference_column_labels()
        by_name: dict[str, int] = {}
        if self.reference_header is not None:
            for i, name in enumerate(self.reference_header):
                by_name.setdefault(name.strip().casefold(), i)
        rows: list[MappingRow] = []
        source_has_header = self._controller.header is not None
        for i, label in enumerate(source):
            ref_index: int | None = None
            if source_has_header and self.reference_header is not None:
                ref_index = by_name.get(label.strip().casefold())
            if ref_index is None and i < len(ref_labels):
                ref_index = i
            rows.append(MappingRow(source_index=i, source_label=label, ref_index=ref_index))
        self.mapping = rows

    def set_mapping(self, source_index: int, ref_index: int | None) -> None:
        for row in self.mapping:
            if row.source_index == source_index:
                row.ref_index = ref_index
                return

    def mapping_ready(self) -> bool:
        return self.reference_path is not None and any(
            row.ref_index is not None for row in self.mapping
        )

    def build_mapping(self) -> ColumnMapping:
        pairs = tuple(
            (row.source_index, row.ref_index) for row in self.mapping if row.ref_index is not None
        )
        return ColumnMapping(pairs=pairs)

    def extract_kwargs(self, rule_id: str, id_column: int) -> dict:
        """Picklable arguments for the extraction subprocess."""
        assert self.reference_path is not None
        source_options = self._state.csv_options
        separator = source_options.separator if source_options is not None else ","
        return {
            "scan_db_path": str(self._controller.db_path),
            "rule_id": rule_id,
            "source_path": str(self._state.file),
            "source_csv_options": source_options,
            "source_header": self._controller.header,
            "source_width": self._controller.width,
            "reference_path": str(self.reference_path),
            "reference_csv_options": self.reference_csv_options,
            "reference_sheet": self.reference_sheet,
            "reference_has_header": self.reference_has_header,
            "mapping": self.build_mapping(),
            "settings": self._ruleset.settings,
            "id_column": id_column,
            "out_separator": separator,
        }


def verify_kwargs_from_extraction(extract_kwargs: dict, ruleset: RuleSet) -> dict:
    """Picklable arguments for the verification subprocess, derived from the
    extraction's arguments so both steps see exactly the same inputs."""
    k = extract_kwargs
    return {
        "scan_db_path": k["scan_db_path"],
        "ruleset": ruleset,
        "rule_id": k["rule_id"],
        "source_path": k["source_path"],
        "source_csv_options": k["source_csv_options"],
        "source_header": k["source_header"],
        "source_width": k["source_width"],
        "dup_file_path": k["out_path"],
        "dup_separator": k["out_separator"],
        "mapping": k["mapping"],
        "settings": k["settings"],
        "ref_db_path": k["ref_db_path"],
    }


class VerifyPresenter:
    """Step 7: paginated, searchable viewer over the verification results."""

    def __init__(
        self,
        db_path: Path,
        *,
        source_header: list[str] | None,
        source_width: int,
        tr: Translator,
        page_size: int = 10,
    ):
        from lsa.core.verify import VerificationStore

        self._store = VerificationStore(db_path, read_only=True)
        self._header = source_header
        self._width = source_width
        self._tr = tr
        self.page_size = max(1, page_size)
        self.offset = 0
        self.filter_text = ""
        self._total: int | None = None

    def close(self) -> None:
        self._store.close()

    # ------------------------------------------------------------- queries

    def total(self) -> int:
        if self._total is None:
            self._total = self._store.count(self.filter_text)
        return self._total

    def set_filter(self, text: str) -> None:
        """Live search over the first two cells of each row."""
        if text == self.filter_text:
            return
        self.filter_text = text
        self.offset = 0
        self._total = None

    def set_page_size(self, size: int) -> None:
        self.page_size = max(1, size)
        self.offset = (self.offset // self.page_size) * self.page_size

    def goto_row(self, seq: int) -> None:
        """Jump to the page containing duplicate-file row ``seq``."""
        position = self._store.position_of(seq, self.filter_text)
        position = max(0, min(position, max(0, self.total() - 1)))
        self.offset = (position // self.page_size) * self.page_size

    def goto_page(self, page: int) -> None:
        last_page = max(0, (self.total() - 1) // self.page_size)
        target = max(0, min(page - 1, last_page))
        self.offset = target * self.page_size

    @property
    def can_prev(self) -> bool:
        return self.offset > 0

    @property
    def can_next(self) -> bool:
        return self.offset + self.page_size < self.total()

    def next_page(self) -> None:
        if self.can_next:
            self.offset += self.page_size

    def prev_page(self) -> None:
        self.offset = max(0, self.offset - self.page_size)

    def position_text(self) -> str:
        total = self.total()
        if total == 0:
            return self._tr("report.no_matches")
        first = self.offset + 1
        last = min(self.offset + self.page_size, total)
        return self._tr("report.position", first=first, last=last, total=total)

    # ------------------------------------------------------------- display

    def table_columns(self) -> list[str]:
        data_columns = (
            list(self._header)
            if self._header is not None
            else [index_to_letter(i) for i in range(self._width)]
        )
        return [
            "#",
            self._tr("verify.status"),
            self._tr("verify.condition"),
            self._tr("verify.occurrences"),
            self._tr("verify.matches"),
            *data_columns,
        ]

    def page_rows(self) -> list[list[str]]:
        rows = []
        for entry in self._store.page(self.offset, self.page_size, self.filter_text):
            status = self._tr("verify.valid") if entry.valid else self._tr("verify.invalid")
            cells = list(entry.cells[: self._width])
            cells += [""] * (self._width - len(cells))
            rows.append(
                [
                    str(entry.seq),
                    status,
                    entry.condition or "-",
                    str(entry.occurrences),
                    entry.matches_display,
                    *cells,
                ]
            )
        return rows

    def summary_text(self, stats) -> str:
        text = self._tr(
            "verify.summary",
            total=f"{stats.total:,}",
            valid_a=f"{stats.valid_a:,}",
            valid_b=f"{stats.valid_b:,}",
            invalid=f"{stats.invalid:,}",
        )
        if stats.malformed_rows:
            text += " " + self._tr("run.malformed", count=f"{stats.malformed_rows:,}")
        return text


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
        # Duplicate rules paginate over key-group BLOCKS (each key combination
        # is an isolated section); plain rules paginate over rows.
        self._pagers: dict[str, MatchPager | GroupPager] = {}
        for rule in ruleset.rules:
            if rule_has_groups(rule):
                self._pagers[rule.id] = GroupPager(store, rule.id, GROUPS_PER_PAGE)
            else:
                self._pagers[rule.id] = MatchPager(store, rule.id, sample_size)

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
        key = "report.groups.position" if isinstance(pager, GroupPager) else "report.position"
        return self._tr(key, first=first, last=last, total=pager.total)

    def _display_row(self, match, grouped: bool, unique_label: str) -> list[str]:
        cells = self._source.cells_for(match)
        padded = list(cells[: self._width]) + [""] * max(0, self._width - len(cells))
        row = [str(match.row_number), *padded]
        if grouped:
            row.insert(1, format_sub_key(match, unique_label))
        return row

    def group_blocks(self, rule_id: str) -> list[GroupBlock]:
        """The current page of key-group blocks (duplicate rules only).

        Each block is one key combination: a header ("AMM & O009KH - 8
        rows"), up to ``sample_size`` rows, and a "+N more" note when the
        block is larger.  A leading block collects matches that belong to
        no group (per-row conditions of OR rules).
        """
        pager = self._pagers[rule_id]
        assert isinstance(pager, GroupPager)
        unique_label = self._tr("report.group.unique")
        blocks = []
        for group_key, count in pager.page():
            matches = self._store.get_group_rows(rule_id, group_key, self.sample_size)
            rows = [self._display_row(m, grouped=True, unique_label=unique_label) for m in matches]
            if group_key is None:
                title = self._tr("report.group.other", count=f"{count:,}")
            else:
                title = self._tr(
                    "report.group.header",
                    values=format_group_key(group_key, separator=" & "),
                    count=f"{count:,}",
                )
            hidden = count - len(rows)
            more = self._tr("report.group.more", count=f"{hidden:,}") if hidden > 0 else None
            blocks.append(GroupBlock(title=title, rows=rows, more_label=more))
        return blocks

    def can_prev(self, rule_id: str) -> bool:
        return self._pagers[rule_id].has_prev

    def can_next(self, rule_id: str) -> bool:
        return self._pagers[rule_id].has_next

    def page_rows(self, rule_id: str) -> list[list[str]]:
        """Display rows for the current page (plain rules only)."""
        pager = self._pagers[rule_id]
        assert isinstance(pager, MatchPager)
        unique_label = self._tr("report.group.unique")
        return [
            self._display_row(match, grouped=False, unique_label=unique_label)
            for match in pager.page()
        ]

    def next_page(self, rule_id: str) -> None:
        self._pagers[rule_id].next()

    def prev_page(self, rule_id: str) -> None:
        self._pagers[rule_id].prev()

    def set_sample_size(self, size: int) -> None:
        """Rows per page (plain rules) / rows shown per block (duplicate rules)."""
        self.sample_size = max(1, size)
        for pager in self._pagers.values():
            if isinstance(pager, MatchPager):
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
