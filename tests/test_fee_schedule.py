import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path


def load_collector():
    path = Path(__file__).resolve().parents[1] / "scripts" / "kalshi_fee_calculator.py"
    spec = importlib.util.spec_from_file_location("kalshi_fee_collector", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FeeScheduleTests(unittest.TestCase):
    def setUp(self):
        self.c = load_collector()
        self.c._series_fee_cache.clear()
        self.oct = datetime(2025, 11, 1, tzinfo=timezone.utc)
        self.jan = datetime(2024, 6, 1, tzinfo=timezone.utc)

    def test_maker_era_charges_both_sides_not_volume_split(self):
        fee_changes = {
            "KXNFLGAME": [
                (datetime(2026, 1, 1, tzinfo=timezone.utc), 1.0, "quadratic_with_maker_fees"),
            ]
        }
        fee = self.c.contract_fees(
            10.0, 0.50, "KXNFLGAME",
            datetime(2026, 2, 1, tzinfo=timezone.utc), fee_changes,
        )
        taker_per = self.c.math.ceil(0.07 * 0.25 * 100) / 100
        maker_per = self.c.math.ceil(0.0175 * 0.25 * 100) / 100
        self.assertAlmostEqual(fee, (taker_per + maker_per) * 10.0)

    def test_taker_only_before_series_maker_rollout(self):
        fee_changes = {
            "KXNFLGAME": [
                (datetime(2026, 1, 1, tzinfo=timezone.utc), 1.0, "quadratic_with_maker_fees"),
            ]
        }
        fee = self.c.contract_fees(10.0, 0.50, "KXNFLGAME", self.jan, fee_changes)
        taker_per = self.c.math.ceil(0.07 * 0.25 * 100) / 100
        self.assertAlmostEqual(fee, taker_per * 10.0)

    def test_candle_uses_mean_dollars_only(self):
        candle = {
            "volume_fp": "5.00",
            "price": {"close_dollars": "0.50", "mean_dollars": "0.48"},
        }
        fee = self.c.candle_fee(candle, "TEST", self.jan, {})
        expected = self.c.contract_fees(5.0, 0.48, "TEST", self.jan, {})
        self.assertAlmostEqual(fee, expected)

    def test_candle_without_mean_dollars_skips_fee(self):
        candle = {"volume_fp": "5.00", "price": {"close_dollars": "0.50"}}
        self.assertEqual(self.c.candle_fee(candle, "TEST", self.jan, {}), 0.0)


if __name__ == "__main__":
    unittest.main()