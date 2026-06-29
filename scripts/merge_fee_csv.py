#!/usr/bin/env python3
"""Merge refreshed daily fee rows into an existing kalshi_fee_daily.csv."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path


def read_daily_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def merge_daily_rows(existing_rows: list[dict], incoming_rows: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for row in existing_rows:
        date = row.get("date", "").strip()
        if date:
            merged[date] = dict(row)
    for row in incoming_rows:
        date = row.get("date", "").strip()
        if date:
            merged[date] = dict(row)
    rows = [merged[date] for date in sorted(merged)]
    cumulative = 0.0
    for row in rows:
        cumulative += float(row.get("daily_fee", 0) or 0)
        row["cumulative_fee"] = round(cumulative, 2)
        row["daily_fee"] = round(float(row.get("daily_fee", 0) or 0), 2)
        for key in list(row):
            if key.startswith("cat_"):
                row[key] = round(float(row[key] or 0), 2)
    return rows


def monthly_rows_from_daily(daily_rows: list[dict]) -> list[dict]:
    totals: dict[str, float] = defaultdict(float)
    for row in daily_rows:
        month = row["date"][:7]
        totals[month] += float(row.get("daily_fee", 0) or 0)
    return [{"month": month, "fee": round(fee, 2)} for month, fee in sorted(totals.items())]


def write_daily_csv(path: Path, rows: list[dict]) -> None:
    cat_cols = sorted({key for row in rows for key in row if key.startswith("cat_")})
    fields = ["date", "daily_fee", "cumulative_fee", *cat_cols]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, 0) for field in fields})


def write_monthly_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["month", "fee"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge refreshed Kalshi fee CSV output into repo files")
    parser.add_argument("--existing", default="kalshi_fee_daily.csv")
    parser.add_argument("--incoming", required=True)
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    merged = merge_daily_rows(
        read_daily_rows(Path(args.existing)),
        read_daily_rows(Path(args.incoming)),
    )
    if not merged:
        raise SystemExit("No rows to write after merge.")

    write_daily_csv(output_dir / "kalshi_fee_daily.csv", merged)
    write_monthly_csv(output_dir / "kalshi_fee_monthly.csv", monthly_rows_from_daily(merged))
    print(f"Merged {len(merged)} daily rows into {output_dir}")


if __name__ == "__main__":
    main()
