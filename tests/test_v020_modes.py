"""v0.2.0 - Mode H1 dan M1 mandiri (strategi, config, bukti perilaku) + regresi hash mode lama."""
import unittest
from dataclasses import replace

from btc_robot.backtest import run_backtest
from btc_robot.config import ConfigError, Config, load_config, validate
from btc_robot.data import generate_synthetic
from btc_robot.models import Side
from btc_robot.mtf_strategy import MultiTimeframeStrategy
from btc_robot.strategies import make_strategy
from btc_robot.strategy import EmaRsiTrendStrategy, H1TrendStrategy, M1ScalpStrategy

H1_CFG = "config.h1.example.json"
M1_CFG = "config.m1.example.json"
MTF_CFG = "config.mtf.example.json"
HOUR, MIN = 3_600_000, 60_000


class ModeConfigTests(unittest.TestCase):
    def test_example_configs_valid_and_distinct(self):
        h1, m1 = validate(load_config(H1_CFG)), validate(load_config(M1_CFG))
        self.assertEqual((h1.interval, h1.strategy_mode), ("1h", "h1_trend"))
        self.assertEqual((m1.interval, m1.strategy_mode), ("1m", "m1_scalp"))
        self.assertNotEqual(h1.strategy, m1.strategy)

    def test_interval_must_match_mode(self):
        with self.assertRaises(ConfigError):
            validate(replace(load_config(H1_CFG), interval="15m"))
        with self.assertRaises(ConfigError):
            validate(replace(load_config(M1_CFG), interval="1h"))

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ConfigError):
            validate(replace(load_config(H1_CFG), strategy_mode="m5_magic"))

    def test_factory_returns_independent_classes(self):
        self.assertIsInstance(make_strategy(validate(load_config(H1_CFG))), H1TrendStrategy)
        self.assertIsInstance(make_strategy(validate(load_config(M1_CFG))), M1ScalpStrategy)
        self.assertIsInstance(make_strategy(validate(load_config(MTF_CFG))), MultiTimeframeStrategy)
        self.assertIsInstance(make_strategy(Config()), EmaRsiTrendStrategy)
        self.assertNotEqual(H1TrendStrategy.VERSION, M1ScalpStrategy.VERSION)


class H1StrategyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = validate(load_config(H1_CFG))
        self.c = generate_synthetic(4000, 5, HOUR)
        self.h1 = H1TrendStrategy(self.cfg.strategy)
        self.h1.prepare(self.c)
        self.base = EmaRsiTrendStrategy(self.cfg.strategy)
        self.base.prepare(self.c)

    def test_h1_signals_are_subset_of_base_with_slope_filter(self):
        n_h1 = n_base = 0
        for i in range(60, len(self.c)):
            a, b = self.h1.signal_at(i), self.base.signal_at(i)
            n_base += b.side != Side.NO_TRADE
            if a.side != Side.NO_TRADE:
                n_h1 += 1
                self.assertEqual(a.side, b.side)
                slope = self.h1._slow[i] - self.h1._slow[i - H1TrendStrategy.SLOPE_BARS]
                self.assertTrue(slope > 0 if a.side == Side.LONG else slope < 0)
                self.assertEqual(a.strategy_version, "h1_trend/1.0.0")
        self.assertGreater(n_h1, 0)
        self.assertLess(n_h1, n_base, "filter kemiringan harus benar-benar menyaring sebagian sinyal dasar")

    def test_no_lookahead(self):
        for i in range(120, 3500, 211):
            part = H1TrendStrategy(self.cfg.strategy)
            part.prepare(self.c[:i + 1])
            a, b = self.h1.signal_at(i), part.signal_at(i)
            self.assertEqual((a.side, a.stop_price, a.take_profit), (b.side, b.stop_price, b.take_profit))

    def test_backtest_runs_and_is_deterministic(self):
        r1 = run_backtest(self.c, self.cfg)
        r2 = run_backtest(self.c, self.cfg)
        self.assertGreater(r1.metrics["trades"], 5)
        self.assertEqual(r1.result_hash, r2.result_hash)

    def test_h1_golden_hash(self):
        """Regresi: berubah hanya bila logika H1/engine/risk berubah (sengaja)."""
        r = run_backtest(generate_synthetic(3000, 1, HOUR), self.cfg)
        self.assertEqual(r.result_hash[:12], "7b023b2cfc65")


class M1StrategyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = validate(load_config(M1_CFG))
        self.c = generate_synthetic(6000, 3, MIN, vol=0.0007)
        self.m1 = M1ScalpStrategy(self.cfg.strategy)
        self.m1.prepare(self.c)

    def test_long_signals_follow_pullback_resume_rule(self):
        n = 0
        for i in range(30, len(self.c)):
            s = self.m1.signal_at(i)
            if s.side == Side.LONG:
                n += 1
                f, sl, pf = self.m1._fast[i], self.m1._slow[i], self.m1._fast[i - 1]
                self.assertGreater(f, sl)
                self.assertLessEqual(self.c[i - 1].close, pf)
                self.assertGreater(self.c[i].close, f)
                self.assertGreater(s.stop_price, 0)
                self.assertLess(s.stop_price, s.ref_price)
                self.assertAlmostEqual((s.take_profit - s.ref_price) / (s.ref_price - s.stop_price),
                                       self.cfg.strategy.reward_risk, places=6)
        self.assertGreater(n, 5)

    def test_atr_floor_filters_quiet_market(self):
        quiet = generate_synthetic(3000, 3, MIN, vol=0.00001)
        m = M1ScalpStrategy(self.cfg.strategy)
        m.prepare(quiet)
        self.assertTrue(all(m.signal_at(i).side == Side.NO_TRADE for i in range(30, len(quiet))))

    def test_integration_exercises_trailing_and_max_hold(self):
        r = run_backtest(generate_synthetic(20000, 1, MIN, vol=0.0007), self.cfg)
        c = r.counters
        self.assertGreater(c["entries"], 10)
        self.assertGreater(c["trailing_updates"], 0)
        self.assertGreater(c["exits_max_hold"], 0)
        self.assertGreater(c["safeguard_reject:cooldown"], 0)

    def test_edge_filter_blocks_all_entries_when_costs_swallow_the_target(self):
        """Biaya ekstrem (0,5%/sisi) > TP: filter edge menolak semua entry. (Kelayakan M1 pada fee nyata
        TIDAK dibuktikan oleh tes ini; itu butuh data pasar 1m sungguhan.)"""
        costly = replace(self.cfg, costs=replace(self.cfg.costs, fee_rate=0.005))
        r = run_backtest(generate_synthetic(20000, 1, MIN, vol=0.0007), costly)
        self.assertEqual(r.counters.get("entries", 0), 0)
        self.assertGreater(r.counters["risk_reject:edge_tidak_cukup_untuk_biaya"], 0)


class LegacyModeRegressionTests(unittest.TestCase):
    def test_mtf_golden_result(self):
        """Regresi. v4: hash 739d19906c93 (83 trade, +10,1998%). v0.3.0: 3d93f705f0b2 (83 trade, +10,2189%).
        Perubahan SENGAJA: cooldown breaker kini dimulai saat rugi ke-3 ditutup (sebelumnya malas, pada sinyal
        berikutnya yang kebetulan dievaluasi sehingga bergantung pada urutan pemeriksaan)."""
        r = run_backtest(generate_synthetic(3000, 1, 900_000), validate(load_config(MTF_CFG)))
        self.assertEqual(r.metrics["trades"], 83)
        self.assertEqual(r.result_hash[:12], "3d93f705f0b2")


if __name__ == "__main__":
    unittest.main()
