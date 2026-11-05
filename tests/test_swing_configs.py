import os
import unittest
from dataclasses import replace

from btc_robot.backtest import run_backtest
from btc_robot.config import load_config, validate
from btc_robot.data import generate_synthetic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MS = {"4h": 14_400_000, "1d": 86_400_000}


def _cfg(market, tf):
    cfg = load_config(os.path.join(ROOT, f"config.{market}_{tf}.example.json"))
    return validate(replace(cfg, state_dir=os.path.join(ROOT, "state", "test_swing")))


class SwingConfigs(unittest.TestCase):
    def test_all_four_load_and_are_safe(self):
        for market in ("spot", "futures"):
            for tf in ("4h", "1d"):
                c = _cfg(market, tf)
                self.assertEqual(c.interval, tf)
                self.assertEqual(c.strategy_mode, {"4h": "d1h4_" + market, "1d": "lab_trend"}[tf])
                self.assertEqual(c.risk.allow_short, market == "futures")
                self.assertLessEqual(c.risk.max_notional_pct_of_equity, 100.0)   # tanpa leverage
                self.assertLessEqual(c.risk.risk_per_trade_pct, 1.0)
                if (market, tf) == ("futures", "4h"):                           # SL/TP tetap (strategi sweep)
                    self.assertEqual(c.risk.trailing_trail_pct, 0)
                    self.assertGreater(c.risk.max_hold_sec, 0)
                else:                                                            # tahan lama, keluar via trailing
                    self.assertGreater(c.risk.trailing_trail_pct, 0)
                    self.assertEqual(c.risk.max_hold_sec, 0)
                self.assertEqual(c.mode.value, "BACKTEST")

    def test_spot_never_shorts_futures_can(self):
        tf = "4h"
        candles = generate_synthetic(3000, seed=3, interval_ms=MS[tf], vol=0.016,
                                     start_ts=1_700_000_000_000 // MS[tf] * MS[tf])
        spot = run_backtest(candles, _cfg("spot", tf))
        fut = run_backtest(candles, _cfg("futures", tf))
        self.assertEqual([t for t in spot.trades if t["side"] == "SHORT"], [])
        self.assertTrue(any(t["side"] == "SHORT" for t in fut.trades))

    def test_state_dirs_are_separate(self):
        dirs = {load_config(os.path.join(ROOT, f"config.{m}_{tf}.example.json")).state_dir
                for m in ("spot", "futures") for tf in ("4h", "1d")}
        self.assertEqual(len(dirs), 4)


if __name__ == "__main__":
    unittest.main()
