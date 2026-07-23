"""Generate large synthetic CSV fixtures with a known number of "bad" rows.

Used by the perf smoke test and for manual testing:

    uv run python scripts/generate_fixture.py --rows 1000000 --out big.csv

The file is written deterministically so the planted match counts are exact:
every ``--bad-email-every``-th data row has an empty e-mail and every
``--same-addr-every``-th row has billing == shipping.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

HEADER = ["id", "name", "email", "billing", "shipping", "note"]


def generate(
    path: str | Path,
    rows: int,
    *,
    bad_email_every: int = 100,
    same_addr_every: int = 250,
    pad: int = 40,
) -> dict[str, int]:
    """Write ``rows`` data rows; returns the planted counts per rule."""
    counts = {"missing-email": 0, "billing-equals-shipping": 0}
    padding = "x" * pad
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        for i in range(1, rows + 1):
            if i % bad_email_every == 0:
                email = ""
                counts["missing-email"] += 1
            else:
                email = f"user{i}@example.com"
            billing = f"{i % 997} Main Street, Springfield"
            if i % same_addr_every == 0:
                shipping = billing
                counts["billing-equals-shipping"] += 1
            else:
                shipping = f"{i % 887} Oak Avenue, Shelbyville"
            writer.writerow([i, f"Name {i}", email, billing, shipping, padding])
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--out", default="fixture.csv")
    parser.add_argument("--bad-email-every", type=int, default=100)
    parser.add_argument("--same-addr-every", type=int, default=250)
    parser.add_argument("--pad", type=int, default=40, help="padding characters per row")
    args = parser.parse_args(argv)
    counts = generate(
        args.out,
        args.rows,
        bad_email_every=args.bad_email_every,
        same_addr_every=args.same_addr_every,
        pad=args.pad,
    )
    size = Path(args.out).stat().st_size
    print(f"wrote {args.rows:,} rows ({size / 1024 / 1024:.1f} MB) to {args.out}")
    for rule, count in counts.items():
        print(f"  planted {rule}: {count:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
