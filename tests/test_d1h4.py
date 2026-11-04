import os
import unittest
from dataclasses import replace

from btc_robot.backtest import run_backtest
from btc_robot.config import ConfigError, load_config, validate
from btc_robot.data import generate_synthetic
from btc_robot.models import Side
from btc_robot.strategies import make_strategy
from btc_robot.strategy_d1h4 import aggregate_d1, futures_setup, spot_setup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H4 = 14_400_000
DAY = 86_400_000


def _cfg(market):
    c = load_config(os.path.join(ROOT, f"config.{market}_4h.example.json"))
    return validate(replace(c, state_dir=os.path.join(ROOT, "state", "test_d1h4")))


def _candles(n=4200, seed=5):
    return generate_synthetic(n, seed=seed, interval_ms=H4, vol=0.016, start_ts=1_700_000_000_000 // DAY * DAY)


def _v(**kw):
    base = dict(close=100.0, low=99.0, high=101.0, prev_low=99.5, prev_high=100.5, ema9=10.0, ema21=9.0, ema50=101.0,
                rsi=35.0, ema9_prev=8.0, ema21_prev=9.0, d1_close=110.0, d1_ema50=100.0, d1_ema200=90.0,
                d1_rsi=60.0, d1_swing_low=95.0)
    base.update(kw)
    return base


class Setups(unittest.TestCase):
    def test_spot_buy_and_rejects(self):
        s = spot_setup(_v(), 2.0)
        self.assertEqual(s[0], Side.LONG)
        self.assertAlmostEqual(s[1], 95.0 * 0.98)
        self.assertIsNone(s[2])
        self.assertIsNone(spot_setup(_v(d1_close=80.0), 2.0))          # di bawah EMA200 D1
        self.assertIsNone(spot_setup(_v(d1_rsi=45.0), 2.0))            # RSI D1 <= 50
        self.assertIsNone(spot_setup(_v(ema9_prev=10.0), 2.0))         # tidak ada cross
        self.assertIsNone(spot_setup(_v(close=105.0, ema50=101.0, rsi=55.0), 2.0))   # tidak diskon

    def test_futures_long_sweep(self):
        s = futures_setup(_v(low=99.0, prev_low=99.5, close=100.0), 2.0)
        self.assertEqual(s[0], Side.LONG)
        self.assertAlmostEqual(s[1], 99.0 * 0.997)
        self.assertAlmostEqual(s[2], 100.0 + 2.0 * (100.0 - 99.0 * 0.997))
        self.assertIsNone(futures_setup(_v(close=99.0), 2.0))          # close tidak kembali di atas low lama
        self.assertIsNone(futures_setup(_v(rsi=45.0), 2.0))

    def test_futures_short_sweep(self):
        v = _v(d1_close=80.0, d1_ema50=100.0, high=101.0, prev_high=100.5, close=100.0, rsi=65.0)
        s = futures_setup(v, 2.0)
        self.assertEqual(s[0], Side.SHORT)
        self.assertAlmostEqual(s[1], 101.0 * 1.003)
        self.assertLess(s[2], 100.0)
        self.assertIsNone(futures_setup(dict(v, rsi=55.0), 2.0))


class Pipeline(unittest.TestCase):
    def test_d1_only_complete_days(self):
        c = _candles(60)
        d1 = aggregate_d1(c[:-3])                                     # hari terakhir tidak lengkap
        self.assertTrue(all(b.ts % DAY == 0 for b in d1))
        self.assertEqual(len(d1), len(aggregate_d1(c[:-3])))
        self.assertLessEqual(d1[-1].ts + DAY, c[-4].ts + H4 + DAY)

    def test_causal_no_lookahead(self):
        c = _candles()
        for mode in ("d1h4_spot", "d1h4_futures"):
            cfg = replace(_cfg("futures"), strategy_mode=mode)
            full = make_strategy(cfg)
            full.prepare(c)
            for i in range(1400, len(c), 97):
                part = make_strategy(cfg)
                part.prepare(c[:i + 1])
                a, b = full.signal_at(i), part.signal_at(i)
                self.assertEqual((a.side, a.stop_price, a.take_profit), (b.side, b.stop_price, b.take_profit))

    def test_backtest_runs_and_sides(self):
        for seed in (1, 2, 3):
            c = _candles(seed=seed)
            spot = run_backtest(c, _cfg("spot"))
            fut = run_backtest(c, _cfg("futures"))
            self.assertTrue(all(t["side"] == "LONG" for t in spot.trades))
            for t in fut.trades:
                self.assertIn(t["side"], ("LONG", "SHORT"))
        self.assertTrue(any(run_backtest(_candles(seed=s), _cfg("futures")).trades for s in range(1, 8)))

    def test_config_guards(self):
        with self.assertRaises(ConfigError):
            validate(replace(_cfg("futures"), interval="1h"))
        spot = _cfg("spot")
        with self.assertRaises(ConfigError):
            validate(replace(spot, risk=replace(spot.risk, allow_short=True)))


if __name__ == "__main__":
    unittest.main()
