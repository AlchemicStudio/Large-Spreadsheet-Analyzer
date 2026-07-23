"""Report model, lazy row resolution for previews, and CSV/JSON exports."""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .columns import index_to_letter
from .csv_stream import CsvOptions, CsvRowFetcher
from .rules import RuleSet
from .scan import ScanSummary
from .store import ResultStore, StoredMatch

REPORT_FILE_VERSION = 1


class MatchRowSource:
    """Lazily resolve the full cells of stored matches.

    Cached cells (workbook formats) are returned directly; CSV matches are
    re-read from the source file by byte offset — nothing is kept in memory.
    """

    def __init__(self, path: str | Path, csv_options: CsvOptions | None = None):
        self._path = path
        self._options = csv_options
        self._fetcher: CsvRowFetcher | None = None

    def cells_for(self, match: StoredMatch) -> list[str]:
        """Return the row cells for one match."""
        if match.cells is not None:
            return match.cells
        if match.byte_offset is None:
            return []
        if self._fetcher is None:
            self._fetcher = CsvRowFetcher(self._path, self._options or CsvOptions())
        return self._fetcher.fetch(match.byte_offset)

    def close(self) -> None:
        if self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None

    def __enter__(self) -> MatchRowSource:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class MatchPager:
    """Pagination over the matches of one rule (Previous / Next in the report)."""

    def __init__(self, store: ResultStore, rule_id: str, page_size: int = 3):
        self._store = store
        self.rule_id = rule_id
        self.page_size = max(1, page_size)
        self.total = store.count(rule_id)
        self.offset = 0

    def page(self) -> list[StoredMatch]:
        """The current page of matches."""
        return self._store.get_page(self.rule_id, self.offset, self.page_size)

    @property
    def has_prev(self) -> bool:
        return self.offset > 0

    @property
    def has_next(self) -> bool:
        return self.offset + self.page_size < self.total

    def next(self) -> list[StoredMatch]:
        if self.has_next:
            self.offset += self.page_size
        return self.page()

    def prev(self) -> list[StoredMatch]:
        self.offset = max(0, self.offset - self.page_size)
        return self.page()

    def set_page_size(self, size: int) -> list[StoredMatch]:
        """Change the sample size, re-anchoring to the current first row."""
        self.page_size = max(1, size)
        self.offset = (self.offset // self.page_size) * self.page_size
        return self.page()

    @property
    def position_label(self) -> str:
        """e.g. "4-6 of 120" (1-based, clamped)."""
        if self.total == 0:
            return "0 of 0"
        first = self.offset + 1
        last = min(self.offset + self.page_size, self.total)
        return f"{first}-{last} of {self.total}"


@dataclass(frozen=True, slots=True)
class RuleReport:
    """Per-rule section of the report."""

    rule_id: str
    label: str
    match_count: int
    sample_row_numbers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ScanReport:
    """Summary of one scan, exportable as JSON."""

    file: str
    sheet: str | None
    rows_scanned: int
    partial: bool
    elapsed_seconds: float
    generated_at: str
    rules: tuple[RuleReport, ...]


def build_report(
    *,
    file: str | Path,
    sheet: str | None,
    ruleset: RuleSet,
    summary: ScanSummary,
    store: ResultStore,
    sample_size: int = 3,
) -> ScanReport:
    """Assemble the report model from a finished (possibly cancelled) scan."""
    rule_reports = []
    for rule in ruleset.rules:
        sample = store.get_page(rule.id, 0, sample_size)
        rule_reports.append(
            RuleReport(
                rule_id=rule.id,
                label=rule.label,
                match_count=summary.counts.get(rule.id, 0),
                sample_row_numbers=tuple(m.row_number for m in sample),
            )
        )
    return ScanReport(
        file=str(file),
        sheet=sheet,
        rows_scanned=summary.rows_scanned,
        partial=summary.cancelled,
        elapsed_seconds=round(summary.elapsed_seconds, 3),
        generated_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        rules=tuple(rule_reports),
    )


def report_to_dict(report: ScanReport) -> dict[str, object]:
    """JSON-compatible form of the report."""
    return {
        "version": REPORT_FILE_VERSION,
        "file": report.file,
        "sheet": report.sheet,
        "rows_scanned": report.rows_scanned,
        "partial": report.partial,
        "elapsed_seconds": report.elapsed_seconds,
        "generated_at": report.generated_at,
        "rules": [
            {
                "id": r.rule_id,
                "label": r.label,
                "match_count": r.match_count,
                "sample_row_numbers": list(r.sample_row_numbers),
            }
            for r in report.rules
        ],
    }


def save_report(report: ScanReport, path: str | Path) -> None:
    """Write the report summary as pretty-printed JSON."""
    Path(path).write_text(
        json.dumps(report_to_dict(report), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def safe_filename(name: str) -> str:
    """Turn a rule id into a safe file name."""
    cleaned = re.sub(r"[^\w.-]+", "_", name).strip("._")
    return cleaned or "rule"


def export_rule_matches(
    store: ResultStore,
    rule_id: str,
    source: MatchRowSource,
    out_path: str | Path,
    *,
    header: list[str] | None,
    width: int,
) -> int:
    """Export all matches of one rule to a CSV file; returns the match count.

    The output has a ``row_number`` column followed by the original row.  When
    the source file has no header, columns are titled by letter (A, B, ...).
    """
    columns = header if header is not None else [index_to_letter(i) for i in range(width)]
    written = 0
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["row_number", *columns])
        for match in store.iter_matches(rule_id):
            cells = source.cells_for(match)
            if len(cells) < width:  # pad ragged rows; never truncate wide ones
                cells = list(cells) + [""] * (width - len(cells))
            writer.writerow([match.row_number, *cells])
            written += 1
    return written


def export_all_matches(
    store: ResultStore,
    ruleset: RuleSet,
    source: MatchRowSource,
    out_dir: str | Path,
    *,
    header: list[str] | None,
    width: int,
) -> dict[str, Path]:
    """Export every rule's matches as ``<rule_id>.csv`` inside ``out_dir``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    used_names: set[str] = set()
    for rule in ruleset.rules:
        base = safe_filename(rule.id)
        name, suffix = base, 2
        while name in used_names:  # distinct ids can sanitize to the same file name
            name = f"{base}-{suffix}"
            suffix += 1
        used_names.add(name)
        path = out / f"{name}.csv"
        export_rule_matches(store, rule.id, source, path, header=header, width=width)
        files[rule.id] = path
    return files
