"""Command-line front end.

Exit codes: 0 = scan completed with no matches, 1 = matches found,
2 = error (bad arguments, unreadable file, invalid rules...).
CLI messages are English only; the GUI is the localized surface.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from lsa import __version__
from lsa.core.columns import ColumnResolutionError
from lsa.core.csv_stream import CsvOptions, detect_csv_options
from lsa.core.excel_stream import FORMAT_MEMORY_WARNING, SheetNotFoundError, needs_memory_warning
from lsa.core.report import MatchRowSource, build_report, export_all_matches, save_report
from lsa.core.rows import FileFormat, RowStream, UnsupportedFormatError, detect_format
from lsa.core.rules import RuleSet, RuleValidationError, load_rules
from lsa.core.scan import ScanProgress, run_scan
from lsa.core.store import ResultStore
from lsa.core.streams import open_stream

EXIT_NO_MATCHES = 0
EXIT_MATCHES_FOUND = 1
EXIT_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lsa",
        description=(
            "Large Spreadsheet Analyzer: scan huge CSV/XLS/XLSX/ODS files "
            "for rows matching user-defined rules. Fully offline."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="scan a file with a rules file")
    run_p.add_argument("--file", required=True, help="spreadsheet file (.csv/.xls/.xlsx/.ods)")
    run_p.add_argument("--rules", required=True, help="rules JSON file")
    run_p.add_argument("--separator", help="CSV field separator (default: auto-detected)")
    run_p.add_argument("--quotechar", help="CSV quote character (default: auto-detected)")
    run_p.add_argument("--encoding", help="CSV text encoding (default: auto-detected)")
    header_group = run_p.add_mutually_exclusive_group()
    header_group.add_argument(
        "--header",
        dest="has_header",
        action="store_true",
        default=None,
        help="treat the first row as a header (default: auto-detected for CSV, on otherwise)",
    )
    header_group.add_argument(
        "--no-header",
        dest="has_header",
        action="store_false",
        help="treat the first row as data",
    )
    run_p.add_argument("--sheet", help="sheet name for workbook formats (default: first sheet)")
    run_p.add_argument(
        "--empty-token",
        action="append",
        dest="empty_tokens",
        metavar="TOKEN",
        help="value treated as empty; may be repeated (overrides the rules file settings)",
    )
    run_p.add_argument("--report", metavar="FILE", help="write the report summary as JSON")
    run_p.add_argument(
        "--export-matches", metavar="DIR", help="export each rule's matches as CSV into DIR"
    )
    run_p.add_argument(
        "--sample",
        type=int,
        default=3,
        metavar="N",
        help="sample row numbers per rule in the report (default: 3)",
    )
    run_p.add_argument("--no-progress", action="store_true", help="disable the progress bar")

    val_p = sub.add_parser("validate-rules", help="validate a rules JSON file")
    val_p.add_argument("rules_file", help="rules JSON file to validate")
    return parser


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_ERROR


def _csv_options(args: argparse.Namespace, path: Path) -> CsvOptions:
    options = detect_csv_options(path)
    if args.separator is not None:
        options = replace(options, separator=args.separator)
    if args.quotechar is not None:
        options = replace(options, quotechar=args.quotechar)
    if args.encoding is not None:
        options = replace(options, encoding=args.encoding)
    if args.has_header is not None:
        options = replace(options, has_header=args.has_header)
    return options


def _make_progress(stream: RowStream, disabled: bool):
    """Build a (callback, close) pair rendering a tqdm bar on stderr."""
    if disabled:
        return None, lambda: None
    from tqdm import tqdm

    byte_mode = stream.total_bytes() is not None
    bar = tqdm(
        total=stream.total_bytes() if byte_mode else stream.total_rows,
        unit="B" if byte_mode else "row",
        unit_scale=byte_mode,
        desc="scanning",
    )
    state = {"last": 0}

    def on_progress(progress: ScanProgress) -> None:
        if progress.finalizing:
            bar.set_description("resolving duplicate groups")
            return
        current = progress.bytes_read if byte_mode else progress.rows_processed
        bar.update((current or 0) - state["last"])
        state["last"] = current or 0
        bar.set_postfix(matches=sum(progress.counts.values()), refresh=False)

    return on_progress, bar.close


def _run(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        return _error(f"file not found: {path}")
    try:
        file_format = detect_format(path)
    except UnsupportedFormatError as exc:
        return _error(str(exc))
    try:
        ruleset: RuleSet = load_rules(args.rules)
    except RuleValidationError as exc:
        return _error(str(exc))
    if args.empty_tokens is not None:
        ruleset = replace(
            ruleset, settings=replace(ruleset.settings, empty_tokens=tuple(args.empty_tokens))
        )
    if args.sample < 1:
        return _error("--sample must be at least 1")

    if needs_memory_warning(path):
        print(f"warning: {FORMAT_MEMORY_WARNING}", file=sys.stderr)

    csv_options = _csv_options(args, path) if file_format is FileFormat.CSV else None
    try:
        stream = open_stream(
            path, csv_options=csv_options, sheet=args.sheet, has_header=args.has_header
        )
    except (SheetNotFoundError, ValueError, OSError) as exc:
        return _error(str(exc))

    progress_callback, close_progress = _make_progress(stream, args.no_progress)
    try:
        with ResultStore() as store:
            try:
                summary = run_scan(stream, ruleset, store, progress_callback=progress_callback)
            except ColumnResolutionError as exc:
                return _error(str(exc))
            finally:
                close_progress()

            sheet = getattr(stream, "sheet_name", None)
            partial = " (partial)" if summary.cancelled else ""
            print(
                f"Scanned {summary.rows_scanned:,} rows in {summary.elapsed_seconds:.1f}s{partial}"
            )
            for rule in ruleset.rules:
                count = summary.counts.get(rule.id, 0)
                print(f"  {rule.id}: {count:,} match(es) - {rule.label}")
            if summary.malformed_rows:
                print(
                    f"warning: {summary.malformed_rows:,} malformed row(s) could not "
                    "be parsed and were skipped (check the separator/quote settings)",
                    file=sys.stderr,
                )

            if args.report or args.export_matches:
                if args.report:
                    report = build_report(
                        file=path,
                        sheet=sheet,
                        ruleset=ruleset,
                        summary=summary,
                        store=store,
                        sample_size=args.sample,
                    )
                    save_report(report, args.report)
                    print(f"Report written to {args.report}")
                if args.export_matches:
                    with MatchRowSource(path, csv_options) as source:
                        files = export_all_matches(
                            store,
                            ruleset,
                            source,
                            args.export_matches,
                            header=stream.header,
                            width=stream.width,
                        )
                    print(f"Matches exported to {args.export_matches} ({len(files)} file(s))")

            any_match = any(summary.counts.values())
            return EXIT_MATCHES_FOUND if any_match else EXIT_NO_MATCHES
    finally:
        stream.close()


def _validate_rules(args: argparse.Namespace) -> int:
    try:
        ruleset = load_rules(args.rules_file)
    except RuleValidationError as exc:
        return _error(str(exc))
    print(f"OK: {len(ruleset.rules)} rule(s), version {ruleset.version}")
    return EXIT_NO_MATCHES


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        return _validate_rules(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR
    except SystemExit:
        raise
    except BaseException as exc:
        # Any failure must exit 2, never leak a traceback with exit 1 (the
        # "matches found" code).  BaseException because python-calamine's
        # Rust panics (pyo3 PanicException) do not subclass Exception.
        return _error(str(exc) or repr(exc))


if __name__ == "__main__":
    sys.exit(main())
