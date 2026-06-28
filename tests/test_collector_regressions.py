import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


COLLECTOR_PATHS = [
    Path("/Users/eligoldfine/Downloads/kalshi_fee_calculator (3).py"),
    Path("/Users/eligoldfine/Documents/kalshifee/scripts/kalshi_fee_calculator.py"),
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

                def fake_paginate(api_path, result_key, params=None):
                    if api_path == "/markets":
                        return iter(
                            [
                                {
                                    "ticker": "WRONG-1",
                                    "series_ticker": "REAL",
                                    "category": "economics",
                                    "open_time": "2026-01-01T00:00:00Z",
                                    "close_time": "2026-01-02T00:00:00Z",
                                }
                            ]
                        )
                    if api_path == "/historical/markets":
                        return iter(
                            [
                                {
                                    "ticker": "BAD-2",
                                    "series_ticker": "HISTREAL",
                                    "category": "weather",
                                    "open_time": "2026-01-01T00:00:00Z",
                                    "close_time": "2026-01-02T00:00:00Z",
                                }
                            ]
                        )
                    return iter(())

                def fake_accumulate(candles, series, category, fee_changes, taker_fraction, daily_series, min_date):
                    seen_series.append(series)
                    return 1.0, 1.0

                with (
                    patch.object(collector, "paginate", side_effect=fake_paginate),
                    patch.object(collector, "batch_fetch_candles_live", return_value={"WRONG-1": [{"end_period_ts": 1}]}),
                    patch.object(collector, "fetch_candles_historical", return_value=[{"end_period_ts": 1}]),
                    patch.object(collector, "accumulate_candles", side_effect=fake_accumulate),
                    patch.object(collector.time, "sleep", return_value=None),
                ):
                    collector.process_event_contracts({}, 0.7, None)

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
                    collector.print_and_save(event, perps, 0.7, output_dir)

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
                    result = collector.process_perps(0.7, None)

                self.assertGreater(result["total_fee"], 0)
                self.assertEqual(result["daily_series"], {})
                self.assertTrue(result["all_time_adjustment"])


if __name__ == "__main__":
    unittest.main()
