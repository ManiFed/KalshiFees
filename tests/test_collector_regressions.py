import importlib.util
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch


COLLECTOR_PATHS = [
    Path(__file__).resolve().parents[1] / "scripts" / "kalshi_fee_calculator.py",
]


def load_collector(path):
    spec = importlib.util.spec_from_file_location(f"kalshi_fee_collector_{abs(hash(path))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CollectorRegressionTests(unittest.TestCase):
    def test_process_event_contracts_uses_api_series_ticker_when_available(self):
        for path in COLLECTOR_PATHS:
            with self.subTest(path=str(path)):
                collector = load_collector(path)
                seen_series = []

                live_market = {
                    "ticker": "WRONG-1",
                    "series_ticker": "REAL",
                    "category": "economics",
                    "open_time": "2026-01-01T00:00:00Z",
                    "close_time": "2026-01-02T00:00:00Z",
                    "volume_fp": "10.00",
                }
                hist_market = {
                    "ticker": "BAD-2",
                    "series_ticker": "HISTREAL",
                    "category": "weather",
                    "open_time": "2026-01-01T00:00:00Z",
                    "close_time": "2026-01-02T00:00:00Z",
                    "volume_fp": "10.00",
                }

                def fake_paginate(api_path, result_key, params=None, start_cursor=""):
                    if api_path == "/markets":
                        return iter([live_market])
                    return iter(())

                def fake_fetch_page(path, result_key, params=None, cursor=""):
                    if path == "/historical/markets" and not cursor:
                        return {"markets": [hist_market], "cursor": ""}
                    return {"markets": [], "cursor": ""}

                def fake_accumulate(candles, series, category, fee_changes, daily_series, min_date):
                    seen_series.append(series)
                    return 1.0, 1.0

                with (
                    patch.object(collector, "paginate", side_effect=fake_paginate),
                    patch.object(collector, "fetch_page", side_effect=fake_fetch_page),
                    patch.object(collector, "batch_fetch_candles_live", return_value={"WRONG-1": [{"end_period_ts": 1}]}),
                    patch.object(collector, "fetch_candles_historical", return_value=[{"end_period_ts": 1}]),
                    patch.object(collector, "accumulate_candles", side_effect=fake_accumulate),
                    patch.object(collector, "save_checkpoint", return_value=None),
                    patch.object(collector.time, "sleep", return_value=None),
                ):
                    collector.process_event_contracts({}, None)

                self.assertEqual(seen_series, ["REAL", "HISTREAL"])

    def test_print_and_save_writes_to_selected_output_dir(self):
        for path in COLLECTOR_PATHS:
            with self.subTest(path=str(path)):
                collector = load_collector(path)
                event = {
                    "total_fee": 12.0,
                    "daily_series": {"2026-01-01": {"sports": 12.0}},
                }
                perps = {"total_fee": 0.0, "daily_series": {}}

                with tempfile.TemporaryDirectory() as output_dir:
                    collector.print_and_save(event, perps, output_dir)

                    self.assertTrue((Path(output_dir) / "kalshi_fee_daily.csv").exists())
                    self.assertTrue((Path(output_dir) / "kalshi_fee_monthly.csv").exists())

    def test_process_perps_keeps_lifetime_estimate_out_of_daily_series(self):
        for path in COLLECTOR_PATHS:
            with self.subTest(path=str(path)):
                collector = load_collector(path)

                with patch.object(
                    collector,
                    "paginate",
                    return_value=iter([{"ticker": "PERP", "volume_notional_dollars": 1000.0}]),
                ):
                    result = collector.process_perps(None)

                self.assertGreater(result["total_fee"], 0)
                self.assertEqual(result["daily_series"], {})
                self.assertTrue(result["all_time_adjustment"])

    def test_checkpoint_roundtrip_preserves_totals(self):
        for path in COLLECTOR_PATHS:
            with self.subTest(path=str(path)):
                collector = load_collector(path)
                daily = defaultdict(
                    lambda: defaultdict(float),
                    {"2026-01-01": defaultdict(float, {"sports": 12.5})},
                )
                with tempfile.TemporaryDirectory() as tmp:
                    checkpoint = Path(tmp) / "scan.json"
                    collector.save_checkpoint(
                        str(checkpoint),
                        {
                            "version": collector.CHECKPOINT_VERSION,
                            "total_markets": 3,
                            "total_fee": 12.5,
                            "daily_series": daily,
                            "historical_cursor": "cursor-abc",
                            "historical_page_ticker": "TICK-1",
                        },
                    )
                    loaded = collector.load_checkpoint(str(checkpoint))
                    self.assertEqual(loaded["total_markets"], 3)
                    self.assertEqual(loaded["historical_cursor"], "cursor-abc")
                    self.assertEqual(loaded["daily_series"]["2026-01-01"]["sports"], 12.5)

    def test_validate_scan_quality_flags_partial_scan(self):
        for path in COLLECTOR_PATHS:
            with self.subTest(path=str(path)):
                collector = load_collector(path)
                warnings = collector.validate_scan_quality({
                    "daily_series": {"2026-04-29": {"sports": 1.0}},
                    "hist_count": 1000,
                    "total_markets": 1000,
                    "total_fee": 1000.0,
                    "historical_complete": False,
                    "skip_live": True,
                })
                self.assertTrue(any("Historical pagination" in w for w in warnings))
                self.assertTrue(any("Live markets were skipped" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
