import os
import tempfile
import unittest
from dataclasses import replace

from btc_robot import gates
from btc_robot.audit import AuditLog, redact, verify_chain, verify_file
from btc_robot.backtest import run_backtest
from btc_robot.config import (Config, ConfigError, CostConfig, Mode, RiskConfig, config_from_dict,
                              config_hash, validate)
from btc_robot.data import DataError, generate_synthetic, validate_candles
from btc_robot.execution import ExecutionRejected, SimulatedExecutionAdapter
from btc_robot.indicators import atr, ema, rsi
from btc_robot.ledger import Ledger
from btc_robot.models import ApprovedOrder, Candle, PortfolioSnapshot, Side, Signal
from btc_robot.risk import RiskEngine, floor_to_step
from btc_robot.strategy import EmaRsiTrendStrategy
from btc_robot.validation import HoldoutReuseError, evaluate_holdout_once, split_holdout

H = 3_600_000
T0 = 1_700_000_000_000 // H * H


def candle(i, o, h, l, c, v=1.0):
    return Candle(T0 + i * H, o, h, l, c, v)


def snap(equity=1000.0, cash=1000.0, peak=1000.0, day_pnl=0.0, open_pos=0):
    return PortfolioSnapshot(T0, equity, cash, peak, equity, day_pnl, open_pos)


def long_sig(ref=30000.0, stop=29700.0, tp=30600.0, ts=T0):
    return Signal(ts, Side.LONG, "t", ref, stop, tp, None, "test/1", "fp")


class TestIndicators(unittest.TestCase):
    def test_ema_seed_and_next(self):
        out = ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)

    def test_rsi_bounds_and_monotonic(self):
        up = rsi([float(i) for i in range(1, 40)], 14)
        self.assertEqual(up[-1], 100.0)
        down = rsi([float(i) for i in range(40, 1, -1)], 14)
        self.assertAlmostEqual(down[-1], 0.0)

    def test_atr_constant_range(self):
        n = 30
        a = atr([11.0] * n, [9.0] * n, [10.0] * n, 14)
        self.assertAlmostEqual(a[-1], 2.0)

    def test_causality_truncation(self):
        c = generate_synthetic(300, 3)
        closes = [x.close for x in c]
        full, cut = ema(closes, 20), ema(closes[:200], 20)
        self.assertEqual(full[:200], cut)
        self.assertEqual(rsi(closes, 14)[:200], rsi(closes[:200], 14))


class TestData(unittest.TestCase):
    def test_detects_problems(self):
        good = [candle(i, 10, 11, 9, 10) for i in range(5)]
        self.assertTrue(validate_candles(good, H).ok)
        dup = good + [good[-1]]
        self.assertFalse(validate_candles(dup, H).ok)
        gap = good[:2] + good[3:]
        self.assertIn("celah", validate_candles(gap, H).summary())
        bad = good[:1] + [candle(1, 10, 9, 11, 10)]
        self.assertIn("OHLC", validate_candles(bad, H).summary())
        back = [good[1], good[0]]
        self.assertIn("mundur", validate_candles(back, H).summary())

    def test_stale(self):
        good = [candle(i, 10, 11, 9, 10) for i in range(5)]
        close_ts = good[-1].ts + H
        self.assertTrue(validate_candles(good, H, now_ms=close_ts + 5_000, max_age_seconds=60).ok)
        self.assertFalse(validate_candles(good, H, now_ms=close_ts + 600_000, max_age_seconds=60).ok)


class TestStrategyNoLookahead(unittest.TestCase):
    def test_signal_invariant_to_future(self):
        cfg = Config()
        data = generate_synthetic(600, 11)
        full = EmaRsiTrendStrategy(cfg.strategy)
        full.prepare(data)
        for cut in (150, 300, 450):
            part = EmaRsiTrendStrategy(cfg.strategy)
            part.prepare(data[:cut + 1])
            a, b = full.signal_at(cut), part.signal_at(cut)
            self.assertEqual((a.side, a.stop_price, a.take_profit), (b.side, b.stop_price, b.take_profit))


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.risk = RiskEngine(self.cfg, Mode.BACKTEST)

    def test_sizing_respects_risk_and_lot(self):
        d, o = self.risk.evaluate(long_sig(), 30000.0, snap(), True)
        self.assertTrue(d.approved, d.reason)
        loss_if_stop = o.qty * (30000.0 - 29700.0)
        self.assertLessEqual(loss_if_stop, 1000.0 * 0.5 / 100 + 1e-9)
        self.assertAlmostEqual(o.qty, floor_to_step(o.qty, self.cfg.costs.lot_step))

    def test_rejections(self):
        r = self.risk
        cases = {
            "data_tidak_valid": lambda: r.evaluate(long_sig(), 30000.0, snap(), False),
            "short_tidak_diizinkan": lambda: r.evaluate(
                Signal(T0, Side.SHORT, "t", 30000.0, 30300.0, 29400.0, None, "t", "f"), 30000.0, snap(), True),
            "posisi_sudah_terbuka": lambda: r.evaluate(long_sig(), 30000.0, snap(open_pos=1), True),
            "sinyal_basi": lambda: r.evaluate(long_sig(), 30000.0, snap(), True, bars_since_signal=3),
            "stop_di_sisi_salah": lambda: r.evaluate(long_sig(stop=30100.0), 30000.0, snap(), True),
            "jarak_stop_di_luar_batas": lambda: r.evaluate(long_sig(stop=29990.0), 30000.0, snap(), True),
            "batas_rugi_harian": lambda: r.evaluate(long_sig(), 30000.0, snap(day_pnl=-25.0), True),
            "stop_tidak_ada": lambda: r.evaluate(
                Signal(T0, Side.LONG, "t", 30000.0, None, None, None, "t", "f"), 30000.0, snap(), True),
        }
        for reason, fn in cases.items():
            d, o = fn()
            self.assertFalse(d.approved, reason)
            self.assertIsNone(o)
            self.assertEqual(d.reason, reason)

    def test_drawdown_latches_halt(self):
        d, _ = self.risk.evaluate(long_sig(), 30000.0, snap(equity=880.0, peak=1000.0), True)
        self.assertFalse(d.approved)
        self.assertTrue(self.risk.halted)
        d2, _ = self.risk.evaluate(long_sig(), 30000.0, snap(), True)  # pulih pun tetap halt
        self.assertTrue(d2.reason.startswith("halted"))

    def test_fail_safe_on_garbage(self):
        d, o = self.risk.evaluate(long_sig(), float("nan"), snap(), True)
        self.assertFalse(d.approved)
        d, o = self.risk.evaluate(None, 30000.0, snap(), True)  # type: ignore[arg-type]
        self.assertFalse(d.approved)
        self.assertTrue(d.reason.startswith("risk_engine_error"))

    def test_kill_switch_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = replace(self.cfg, state_dir=td)
            r = RiskEngine(cfg, Mode.BACKTEST)
            open(cfg.kill_switch_file, "w").close()
            d, _ = r.evaluate(long_sig(), 30000.0, snap(), True)
            self.assertEqual(d.reason, "kill_switch_file")

    def test_live_mode_never_approves(self):
        r = RiskEngine(self.cfg, Mode.LIVE)
        d, o = r.evaluate(long_sig(), 30000.0, snap(), True)
        self.assertFalse(d.approved)


class TestExecution(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.risk = RiskEngine(self.cfg, Mode.BACKTEST)
        self.ad = SimulatedExecutionAdapter(self.cfg.costs, Mode.BACKTEST, self.risk.verify)
        _, self.order = self.risk.evaluate(long_sig(), 30000.0, snap(), True)

    def test_forged_order_rejected(self):
        forged = ApprovedOrder("x", Side.LONG, 1.0, 30000.0, 29000.0, None, T0, "BACKTEST", "00" * 32)
        with self.assertRaises(ExecutionRejected):
            self.ad.submit_entry(forged, candle(1, 30000, 30100, 29900, 30050))

    def test_tampered_qty_rejected(self):
        t = replace(self.order, qty=self.order.qty * 10)
        with self.assertRaises(ExecutionRejected):
            self.ad.submit_entry(t, candle(1, 30000, 30100, 29900, 30050))

    def test_duplicate_is_idempotent(self):
        bar = candle(1, 30000, 30100, 29900, 30050)
        f1 = self.ad.submit_entry(self.order, bar)
        f2 = self.ad.submit_entry(self.order, bar)
        self.assertIs(f1, f2)
        self.assertEqual(self.ad.duplicates_blocked, 1)

    def test_long_entry_slippage_adverse_and_fee(self):
        f = self.ad.submit_entry(self.order, candle(1, 30000, 30100, 29900, 30050))
        self.assertGreater(f.price, 30000)
        self.assertAlmostEqual(f.fee, f.qty * f.price * self.cfg.costs.fee_rate)

    def test_sim_adapter_refuses_live_mode(self):
        with self.assertRaises(ExecutionRejected):
            SimulatedExecutionAdapter(self.cfg.costs, Mode.LIVE, self.risk.verify)

    def test_stop_wins_when_both_hit_and_gap_fills_at_open(self):
        led = Ledger(1000.0)
        bar = candle(1, 30000, 30100, 29900, 30050)
        f = self.ad.submit_entry(self.order, bar)
        led.apply_entry(f, self.order)
        both = candle(2, 30000, 30700, 29600, 30000)  # stop 29700 dan TP 30600 kena
        ex = self.ad.check_exit(led.position, both)
        self.assertEqual(ex.reason, "stop")
        gap = candle(2, 29500, 29600, 29400, 29550)  # gap turun melewati stop
        ex = self.ad.check_exit(led.position, gap)
        self.assertLess(ex.price, 29500)  # diisi di open (dikurangi slippage), bukan di 29700


class TestLedger(unittest.TestCase):
    def test_roundtrip_accounting_long_and_short(self):
        cfg = Config()
        for side, stop, tp, exit_px in ((Side.LONG, 29700.0, 30600.0, 30600.0),
                                        (Side.SHORT, 30300.0, 29400.0, 29400.0)):
            cfg2 = replace(cfg, risk=replace(cfg.risk, allow_short=True))
            risk = RiskEngine(cfg2, Mode.BACKTEST)
            ad = SimulatedExecutionAdapter(cfg2.costs, Mode.BACKTEST, risk.verify)
            sig = Signal(T0, side, "t", 30000.0, stop, tp, None, "t", "f")
            _, order = risk.evaluate(sig, 30000.0, snap(), True)
            led = Ledger(1000.0)
            fill = ad.submit_entry(order, candle(1, 30000, 30100, 29900, 30000))
            led.apply_entry(fill, order)
            ex = ad.check_exit(led.position, candle(2, 30000, 30700, 29300, 30000))
            if ex is None or ex.reason == "stop":
                ex = ad.close_at(led.position, exit_px, T0 + 2 * H, "manual")
            trade = led.apply_exit(ex)
            self.assertAlmostEqual(led.cash, 1000.0 + trade["net_pnl"], places=6)
            self.assertAlmostEqual(led.equity(30000.0), led.cash)
            self.assertIsNone(led.position)

    def test_reconcile_and_persistence(self):
        led = Ledger(1000.0)
        self.assertTrue(led.reconcile(0.0))
        self.assertFalse(led.reconcile(0.1))
        again = Ledger.from_dict(led.to_dict())
        self.assertEqual(again.cash, led.cash)


class TestBacktest(unittest.TestCase):
    def test_reproducible_and_costs_matter(self):
        data = generate_synthetic(2500, 5)
        cfg = Config()
        a, b = run_backtest(data, cfg), run_backtest(data, cfg)
        self.assertEqual(a.result_hash, b.result_hash)
        self.assertGreater(a.metrics["trades"], 0)
        free = replace(cfg, costs=CostConfig(fee_rate=0.0, spread_bps=0.0, slippage_bps=0.0))
        c = run_backtest(data, free)
        self.assertGreater(c.metrics["net_pnl"], a.metrics["net_pnl"])
        self.assertGreater(a.metrics["total_fees"], 0)

    def test_no_negative_cash_and_pos_closed(self):
        res = run_backtest(generate_synthetic(2500, 9), Config())
        self.assertTrue(all(eq > 0 for _, eq in res.equity_curve))
        self.assertTrue(verify_chain(res.audit.entries))

    def test_bad_data_refused(self):
        data = generate_synthetic(300, 2)
        del data[100]
        with self.assertRaises(DataError):
            run_backtest(data, Config())

    def test_holdout_single_use(self):
        data = generate_synthetic(1500, 4)
        _, hold = split_holdout(data)
        with tempfile.TemporaryDirectory() as td:
            marker = os.path.join(td, "m.json")
            evaluate_holdout_once(hold, Config(), marker)
            with self.assertRaises(HoldoutReuseError):
                evaluate_holdout_once(hold, Config(), marker)


class TestConfigAndGates(unittest.TestCase):
    def test_live_and_testnet_locked(self):
        with self.assertRaises(ConfigError):
            validate(replace(Config(), mode=Mode.TESTNET))
        with self.assertRaises(ConfigError) as cm:
            validate(replace(Config(), mode=Mode.LIVE), env={})
        self.assertIn("LIVE terkunci", str(cm.exception))

    def test_live_stays_locked_even_with_phrase_but_no_gates(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = replace(Config(), mode=Mode.LIVE, gates_file=os.path.join(td, "g.json"))
            with self.assertRaises(ConfigError) as cm:
                validate(cfg, env={"BTC_ROBOT_LIVE_CONFIRM": "I-ACCEPT-REAL-MONEY-RISK"})
            self.assertIn("gerbang", str(cm.exception))

    def test_unknown_key_and_bad_ranges(self):
        with self.assertRaises(ConfigError):
            config_from_dict({"riks": {}})
        with self.assertRaises(ConfigError):
            validate(replace(Config(), risk=RiskConfig(risk_per_trade_pct=5.0)))
        with self.assertRaises(ConfigError):
            validate(replace(Config(), risk=RiskConfig(max_notional_pct_of_equity=300.0)))

    def test_config_hash_changes(self):
        self.assertNotEqual(config_hash(Config()), config_hash(replace(Config(), initial_equity=2000.0)))


class TestAudit(unittest.TestCase):
    def test_redaction_by_key(self):
        r = redact({"api_key": "abc", "nested": {"Secret": "x", "tx_hash": "0x" + "a" * 64}})
        self.assertEqual(r["api_key"], "[REDACTED]")
        self.assertEqual(r["nested"]["Secret"], "[REDACTED]")
        self.assertTrue(r["nested"]["tx_hash"].startswith("0x"))

    def test_chain_detects_tamper(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "a.jsonl")
            a = AuditLog(p, "h")
            a.log("e1", 1, x=1)
            a.log("e2", 2, x=2)
            self.assertTrue(verify_file(p))
            with open(p, encoding="utf-8") as fh:
                text = fh.read().replace('"x": 1', '"x": 9').replace('"x":1', '"x":9')
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
            self.assertFalse(verify_file(p))


if __name__ == "__main__":
    unittest.main()
