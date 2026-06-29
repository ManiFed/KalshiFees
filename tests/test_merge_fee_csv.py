import csv
import subprocess
import tempfile
import unittest
from pathlib import Path


class MergeFeeCsvTests(unittest.TestCase):
    def test_merge_preserves_history_and_overwrites_recent_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "existing.csv"
            incoming = root / "incoming.csv"
            out = root / "out"
            out.mkdir()

            with existing.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["date", "daily_fee", "cumulative_fee", "cat_sports"])
                writer.writeheader()
                writer.writerow({"date": "2026-01-01", "daily_fee": 10, "cumulative_fee": 10, "cat_sports": 10})
                writer.writerow({"date": "2026-01-02", "daily_fee": 20, "cumulative_fee": 30, "cat_sports": 20})

            with incoming.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["date", "daily_fee", "cumulative_fee", "cat_sports"])
                writer.writeheader()
                writer.writerow({"date": "2026-01-02", "daily_fee": 25, "cumulative_fee": 25, "cat_sports": 25})
                writer.writerow({"date": "2026-01-03", "daily_fee": 5, "cumulative_fee": 30, "cat_sports": 5})

            script = Path(__file__).resolve().parents[1] / "scripts" / "merge_fee_csv.py"
            subprocess.run(
                [
                    "python3",
                    str(script),
                    "--existing",
                    str(existing),
                    "--incoming",
                    str(incoming),
                    "--output-dir",
                    str(out),
                ],
                check=True,
            )

            with (out / "kalshi_fee_daily.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual([row["date"] for row in rows], ["2026-01-01", "2026-01-02", "2026-01-03"])
            self.assertEqual(float(rows[1]["daily_fee"]), 25.0)
            self.assertEqual(float(rows[-1]["cumulative_fee"]), 40.0)


if __name__ == "__main__":
    unittest.main()
