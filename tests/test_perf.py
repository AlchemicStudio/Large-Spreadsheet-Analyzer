"""Performance smoke test on a 1M-row synthetic CSV (opt-in: pytest -m perf).

Asserts correctness of the counts on a large file, that the scan finishes in
a sane time, and that the additional peak RSS stays far below the 500 MB
budget the spec sets for 1 GB inputs.
"""

import importlib.util
import resource
import sys
import time
from pathlib import Path

import pytest

from lsa.core.csv_stream import CsvOptions, CsvRowFetcher, CsvRowStream
from lsa.core.rules import ruleset_from_dict
from lsa.core.scan import run_scan
from lsa.core.store import ResultStore

pytestmark = pytest.mark.perf

ROWS = 1_000_000

RULES = {
    "version": 1,
    "rules": [
        {
            "id": "missing-email",
            "label": "Missing e-mail",
            "match": "all",
            "conditions": [{"type": "is_empty", "column": {"by": "header", "value": "email"}}],
        },
        {
            "id": "billing-equals-shipping",
            "label": "Billing equals shipping",
            "match": "all",
            "conditions": [
                {
                    "type": "equals_column",
                    "column": {"by": "header", "value": "billing"},
                    "other_column": {"by": "header", "value": "shipping"},
                }
            ],
        },
    ],
}


def _load_generator():
    path = Path(__file__).parent.parent / "scripts" / "generate_fixture.py"
    spec = importlib.util.spec_from_file_location("generate_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is KB on Linux, bytes on macOS.
    return peak / 1024 if sys.platform != "darwin" else peak / (1024 * 1024)


def test_million_row_scan(tmp_path_factory) -> None:
    fixture_dir = tmp_path_factory.mktemp("perf")
    csv_path = fixture_dir / "big.csv"
    generator = _load_generator()
    planted = generator.generate(csv_path, ROWS)
    size_mb = csv_path.stat().st_size / 1024 / 1024
    assert size_mb > 50, "fixture should be a genuinely large file"

    ruleset = ruleset_from_dict(RULES)
    options = CsvOptions(has_header=True)
    rss_before = _peak_rss_mb()
    started = time.perf_counter()
    with (
        CsvRowStream(csv_path, options) as stream,
        ResultStore(fixture_dir / "results.sqlite3") as store,
    ):
        summary = run_scan(stream, ruleset, store, progress_interval_rows=100_000)
        elapsed = time.perf_counter() - started
        rss_delta = _peak_rss_mb() - rss_before

        assert summary.rows_scanned == ROWS
        assert summary.counts == planted
        assert store.count("missing-email") == planted["missing-email"]

        # spot-check a stored offset resolves back to the right row
        match = store.get_page("missing-email", 0, 1)[0]
        with CsvRowFetcher(csv_path, options) as fetcher:
            cells = fetcher.fetch(match.byte_offset)
        assert cells[0] == str(match.row_number - 1)  # id == data row index (header is row 1)
        assert cells[2] == ""

    print(
        f"\nscanned {ROWS:,} rows ({size_mb:.0f} MB) in {elapsed:.1f}s, "
        f"peak RSS delta {rss_delta:.0f} MB"
    )
    assert elapsed < 120, f"scan too slow: {elapsed:.1f}s"
    assert rss_delta < 500, f"scan used too much memory: {rss_delta:.0f} MB"
