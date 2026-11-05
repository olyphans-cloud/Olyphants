"""Tes regresi untuk perbaikan v0.1.1: cooldown, celah data, warm-up, lock, performa."""
import os
import random
import tempfile
import unittest
from dataclasses import replace

from btc_robot.backtest import run_backtest
from btc_robot.config import ConfigError, Mode, config_from_dict, validate
from btc_robot.data import generate_synthetic
from btc_robot.models import Candle
from btc_robot.mtf_strategy import H1, M30, MultiTimeframeStrategy
from btc_robot.paper import paper_step
from btc_robot.validation import required_warmup

M15 = 900_000
START_TS = 1_700_000_100_000 // M15 * M15


def mtf_cfg(**risk):
    r = {"max_data_age_seconds": 900}
    r.update(risk)
    return config_from_dict({"mode": "BACKTEST", "interval": "15m", "strategy_mode": "mtf_1h_30m_15m",
                             "risk": r})


class CooldownConfigTests(unittest.TestCase):
    def test_default_cooldown_is_longer_than_a_candle(self):
        cfg = validate(mtf_cfg())
        self.assertGreaterEqual(cfg.risk.loss_streak_cooldown_sec, cfg.interval_ms // 1000)

    def test_cooldown_shorter_than_candle_rejected(self):
        with self.assertRaises(ConfigError):
            validate(mtf_cfg(loss_streak_cooldown_sec=300))

    def test_cooldown_actually_blocks_following_signals(self):
        c = generate_synthetic(4000, 1, M15, start_ts=START_TS)
        res = run_backtest(c, validate(mtf_cfg()))
        self.assertGreater(res.counters.get("safeguard_reject:cooldown", 0), 0)


class PaperGapAndLockTests(unittest.TestCase):
    def setUp(self):
        self.c = generate_synthetic(2200, 11, M15, start_ts=START_TS)
        self.tmp = tempfile.TemporaryDirectory()
        base = mtf_cfg()
        self.cfg = replace(base, mode=Mode.PAPER, state_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def step(self, k):
        c = self.c
        return paper_step(self.cfg, now_ms=c[k].ts + M15 + 15_000,
                          fetcher=lambda: c[max(0, k - 499):k + 1])

    def _run_until(self, want_position, start=700):
        k = start
        r = self.step(k)
        while True:
            k += 1
            r = self.step(k)
            if (r["position"] is not None) == want_position and k > start + 1:
                return k, r
            self.assertLess(k, 2000, "kondisi tidak tercapai pada data uji")

    def test_gap_with_open_position_halts(self):
        k, _ = self._run_until(True)
        r = self.step(k + 700)  # cron mati 700 candle > jendela 500
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["halted"])
        self.assertGreater(r["gap_missed_bars"], 0)

    def test_gap_without_position_does_not_halt(self):
        k, _ = self._run_until(False)
        r = self.step(k + 700)
        self.assertEqual(r["status"], "ok")
        self.assertFalse(r["halted"])
        self.assertGreater(r["gap_missed_bars"], 0)

    def test_no_gap_in_normal_operation(self):
        self.step(700)
        r = self.step(701)
        self.assertEqual(r["gap_missed_bars"], 0)

    def test_concurrent_run_is_skipped_by_lock(self):
        try:
            import fcntl
        except ImportError:
            self.skipTest("fcntl tidak tersedia")
        os.makedirs(self.cfg.state_dir, exist_ok=True)
        with open(os.path.join(self.cfg.state_dir, ".paper.lock"), "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = self.step(700)
            fcntl.flock(fh, fcntl.LOCK_UN)
        self.assertEqual(r["status"], "locked")
        self.assertFalse(os.path.exists(os.path.join(self.cfg.state_dir, "paper_state.json")))


class WarmupAndPerformanceTests(unittest.TestCase):
    def test_mtf_required_warmup_covers_first_signal(self):
        cfg = validate(mtf_cfg())
        c = generate_synthetic(3000, 7, M15, start_ts=START_TS)
        s = MultiTimeframeStrategy(cfg.strategy)
        s.prepare(c)
        first = next(i for i in range(len(c))
                     if not s.signal_at(i).reason.startswith(("warmup", "mtf_warmup")))
        self.assertGreaterEqual(required_warmup(cfg), first - 8)  # margin 8 candle batas jam

    def test_latest_closed_matches_naive_scan(self):
        rnd = random.Random(5)
        for interval in (M30, H1):
            ts = sorted({START_TS + rnd.randrange(0, 5000) * interval for _ in range(300)})
            bars = [Candle(t, 1, 1, 1, 1, 1) for t in ts]
            for _ in range(500):
                close_time = START_TS + rnd.randrange(0, 5200) * interval + rnd.choice((0, M15))
                naive = -1
                for j, b in enumerate(bars):
                    if b.ts + interval <= close_time:
                        naive = j
                    else:
                        break
                self.assertEqual(MultiTimeframeStrategy._latest_closed(ts, close_time, interval), naive)


if __name__ == "__main__":
    unittest.main()
