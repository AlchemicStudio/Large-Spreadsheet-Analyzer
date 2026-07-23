# Large Spreadsheet Analyzer

A local, offline desktop tool (GUI + CLI) that scans very large spreadsheet
files — 500 MB, 800 MB, 1 GB and more — and finds rows matching user-defined
rules ("bad rows to fix"). Supported input formats: **CSV, XLS, XLSX, ODS**.
Nothing ever leaves your machine.

## Highlights

- **Single streaming pass**: the file is read once and every rule is
  evaluated on each row. A 1M-row / ~140 MB CSV scans in a few seconds with
  a flat memory profile (matches are stored in SQLite on disk, not in RAM).
- **Rules**: flat groups of conditions (`is_empty`, `not_empty`,
  `equals_column`) combined with AND/OR, referencing columns by letter (`C`),
  0-based index (`2`) or header name (`email`). Rules are saved/loaded as
  versioned JSON (see `docs/rules.example.json`).
- **GUI**: a five-step CustomTkinter wizard (file → import options → rules →
  run → report) translated into English, French, German, Spanish and
  Portuguese (pt-PT). Scans run in a worker thread with live per-rule
  counters, a global progress bar and a Cancel button (partial results are
  kept and marked as partial).
- **CLI**: the same engine headless, for pipelines
  (exit code `0` = no matches, `1` = matches found, `2` = error).
- **Report**: per-rule match counts with lazily loaded sample rows
  (Previous/Next paging over *all* matches), CSV export per rule or for all
  rules, JSON report summary.

## Format caveats

- **CSV** is streamed row by row; row previews later `seek()` directly to
  the stored byte offset. (For encodings where byte offsets are unreliable —
  UTF-16/32, CR-only line endings — matched rows are cached in SQLite
  instead.)
- **XLSX** is streamed with openpyxl in read-only mode (constant memory).
- **XLS/ODS** cannot be read row-by-row: python-calamine materializes the
  sheet in memory. The app warns when opening large XLS/ODS files — convert
  to CSV or XLSX for best results.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync                 # create venv + install deps
uv run pytest           # unit tests
uv run pytest -m gui    # GUI smoke tests (needs a display; use xvfb-run on headless Linux)
uv run pytest -m perf   # 1M-row performance smoke test
uv run ruff check       # lint
uv run lsa --help       # CLI
uv run lsa-gui          # GUI
```

### CLI examples

```bash
lsa run --file data.csv --rules rules.json --separator ";" --encoding utf-8 \
        --report report.json --export-matches out_dir/ --sample 5
lsa validate-rules rules.json
```

### Generate a large test file

```bash
uv run python scripts/generate_fixture.py --rows 1000000 --out big.csv
```

## Packaging

PyInstaller builds a folder with both executables (`lsa-gui`, `lsa`):

```bash
uv sync --group build
uv run pyinstaller packaging/lsa.spec --noconfirm   # output in dist/lsa/
```

`--collect-all customtkinter` (done inside the spec) is required — without it
the CustomTkinter theme assets are missing at runtime.

## CI/CD

- `ci.yml`: ruff + pytest on Ubuntu/Windows/macOS × Python 3.11/3.12.
- `release.yml`: on `v*` tags, PyInstaller builds for Windows, macOS and
  Linux are attached to the GitHub release (Windows is the priority target).

## Architecture

```
src/lsa/core/   file readers (RowStream), rules, evaluation, SQLite result
                store, report model, JSON (de)serialization, i18n catalogs
src/lsa/cli/    argparse front end
src/lsa/gui/    CustomTkinter wizard; testable presenters, worker thread +
                queue polled with after() (Tk is only touched from the main
                thread)
```
