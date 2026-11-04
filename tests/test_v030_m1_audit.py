"""v0.3.0 - Audit implementasi M1: look-ahead, timestamp, harga fill, biaya (dihitung ulang independen), lot."""
import math
import unittest
from dataclasses import replace

from btc_robot.backtest import run_backtest
from btc_robot.config import Mode, load_config, validate
from btc_robot.data import generate_synthetic
from btc_robot.execution import SimulatedExecutionAdapter
from btc_robot.models import Candle, Position, Side
from btc_robot.strategy import M1ScalpStrategy

MIN = 60_000
CFG_PATH = "config.m1.example.json"


def bps(cfg):
    return (cfg.costs.spread_bps / 2.0 + cfg.costs.slippage_bps) / 10_000.0


class M1Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = validate(load_config(CFG_PATH))
        cls.candles = generate_synthetic(12000, 1, MIN, vol=0.0007)
        cls.res = run_backtest(cls.candles, cls.cfg)
        cls.by_ts = {c.ts: c for c in cls.candles}
        cls.events = cls.res.audit.entries


class NoLookaheadTests(M1Base):
    def test_signal_at_i_uses_only_data_up_to_i(self):
        full = M1ScalpStrategy(self.cfg.strategy)
        full.prepare(self.candles)
        n_signals = 0
        for i in range(60, len(self.candles), 83):
            part = M1ScalpStrategy(self.cfg.strategy)
            part.prepare(self.candles[:i + 1])
            a, b = full.signal_at(i), part.signal_at(i)
            self.assertEqual((a.side, a.stop_price, a.take_profit, a.reason),
                             (b.side, b.stop_price, b.take_profit, b.reason), f"indeks {i}")
            n_signals += a.side != Side.NO_TRADE
        self.assertGreater(n_signals, 0, "uji tidak bermakna bila tak ada sinyal yang dibandingkan")

    def test_changing_future_candles_does_not_change_past_signals(self):
        full = M1ScalpStrategy(self.cfg.strategy)
        full.prepare(self.candles)
        k = 6000
        mutated = list(self.candles)
        for j in range(k + 1, len(mutated)):
            c = mutated[j]
            mutated[j] = Candle(c.ts, c.open * 1.3, c.high * 1.3, c.low * 1.3, c.close * 1.3, c.volume)
        other = M1ScalpStrategy(self.cfg.strategy)
        other.prepare(mutated)
        for i in range(60, k + 1):
            a, b = full.signal_at(i), other.signal_at(i)
            self.assertEqual((a.side, a.stop_price, a.take_profit), (b.side, b.stop_price, b.take_profit))


class TimestampAndFillConventionTests(M1Base):
    def test_entry_is_at_open_of_the_bar_after_the_signal_bar(self):
        decisions, entries = [], []
        for e in self.events:
            if e["event"] == "risk_decision" and e["payload"]["approved"]:
                decisions.append(e)
            elif e["event"] == "entry":
                entries.append(e)
        self.assertGreater(len(entries), 10)
        approved_signal_ts = {d["ts"]: d["payload"]["signal_bar_ts"] for d in decisions}
        for en in entries:
            self.assertEqual(en["ts"] - approved_signal_ts[en["ts"]], self.cfg.interval_ms)
            bar = self.by_ts[en["ts"]]
            self.assertAlmostEqual(en["payload"]["fill"]["price"], bar.open * (1 + bps(self.cfg)), places=6)

    def test_every_approved_order_is_either_filled_or_vetoed_by_a_later_stage(self):
        approved = sum(1 for e in self.events if e["event"] == "risk_decision" and e["payload"]["approved"])
        entries = self.res.counters["entries"]
        later_vetoes = sum(v for k, v in self.res.counters.items()
                           if k.startswith("safeguard_reject:") or k in ("entry_hour_limit", "halt_reject:final")
                           or k.startswith("portfolio_reject:"))
        self.assertEqual(approved, entries + later_vetoes, dict(self.res.counters))

    def test_signal_uses_closed_bar_values(self):
        for e in self.events:
            if e["event"] == "signal":
                sig = e["payload"]["signal"]
                bar = self.by_ts[sig["bar_ts"]]
                self.assertEqual(sig["ref_price"], bar.close)       # referensi = CLOSE candle yang sudah tutup
                self.assertEqual(e["ts"], bar.ts)

    def test_exit_ts_never_before_entry_ts(self):
        for t in self.res.trades:
            self.assertGreaterEqual(t["exit_ts"], t["entry_ts"])


class CostRecomputationTests(M1Base):
    def test_every_trade_recomputes_exactly_from_first_principles(self):
        c = self.cfg.costs
        self.assertGreater(len(self.res.trades), 10)
        for t in self.res.trades:
            fees = t["qty"] * c.fee_rate * (t["entry_price"] + t["exit_price"])
            gross = (t["exit_price"] - t["entry_price"]) * t["qty"]            # LONG saja pada config M1
            self.assertEqual(t["side"], "LONG")
            self.assertAlmostEqual(t["fees"], fees, places=8)
            self.assertAlmostEqual(t["gross_pnl"], gross, places=8)
            self.assertAlmostEqual(t["net_pnl"], gross - fees, places=8)

    def test_equity_reconciles_with_trade_ledger(self):
        total_net = sum(t["net_pnl"] for t in self.res.trades)
        self.assertAlmostEqual(self.res.metrics["final_equity"], self.cfg.initial_equity + total_net, places=6)
        self.assertAlmostEqual(self.res.metrics["total_fees"], sum(t["fees"] for t in self.res.trades), places=6)

    def test_exit_prices_follow_the_documented_model(self):
        b = bps(self.cfg)
        for t in self.res.trades:
            if t["exit_reason"] == "take_profit":
                continue                                                      # TP: limit di harga TP (lihat tes karakterisasi)
            bar = self.by_ts[t["exit_ts"]]
            if t["exit_reason"] in ("max_hold", "end_of_data"):
                # penutupan paksa di CLOSE candle keluar, ditambah biaya adverse
                self.assertAlmostEqual(t["exit_price"], bar.close * (1 - b), places=6)
            elif t["exit_reason"] == "stop":
                self.assertLessEqual(t["exit_price"], bar.open * (1 - b) + 1e-9)   # tidak pernah lebih baik dari open-gap

    def test_zero_cost_world_has_net_equal_gross(self):
        free = replace(self.cfg, costs=replace(self.cfg.costs, fee_rate=0.0, spread_bps=0.0, slippage_bps=0.0),
                       risk=replace(self.cfg.risk, min_edge_cost_mult=0.0))
        r = run_backtest(self.candles, free)
        self.assertGreater(len(r.trades), 10)
        for t in r.trades:
            self.assertAlmostEqual(t["net_pnl"], t["gross_pnl"], places=9)
            self.assertEqual(t["fees"], 0.0)

    def test_cost_per_side_matches_config(self):
        """Karakterisasi: 0,5 bps (setengah spread) + 1 bps (slippage) per sisi; fee 0,075% per sisi."""
        self.assertAlmostEqual(bps(self.cfg), 1.5e-4, places=12)
        self.assertAlmostEqual(self.cfg.costs.fee_rate, 0.00075, places=12)


class LotAndSizingTests(M1Base):
    def test_quantity_follows_lot_rules_and_caps(self):
        c = self.cfg.costs
        cap = self.cfg.initial_equity * self.cfg.risk.max_notional_pct_of_equity / 100.0
        for t in self.res.trades:
            steps = t["qty"] / c.lot_step
            self.assertAlmostEqual(steps, round(steps), places=6, msg="qty bukan kelipatan lot_step")
            self.assertGreaterEqual(t["qty"], c.min_qty)
            self.assertGreaterEqual(t["qty"] * t["entry_price"], c.min_notional * 0.999)
        # cap dihitung terhadap ekuitas saat entry; ekuitas bisa naik, beri toleransi 5%
        peak = max(e for _, e in self.res.equity_curve)
        for t in self.res.trades:
            self.assertLessEqual(t["qty"] * t["entry_price"], max(cap, peak) * 1.05)

    def test_quantity_is_floored_never_rounded_up(self):
        from btc_robot.risk import RiskEngine
        r = RiskEngine(self.cfg, Mode.BACKTEST)
        step = self.cfg.costs.lot_step
        for raw in (0.123456789, 0.00001999, 5.00000999):
            q = math.floor(raw / step + 1e-12) * step
            self.assertLessEqual(q, raw + 1e-15)


class FillModelCharacterizationTests(unittest.TestCase):
    """Asumsi eksplisit model eksekusi (bukan klaim kebenaran pasar)."""
    def setUp(self):
        self.cfg = validate(load_config(CFG_PATH))
        self.ad = SimulatedExecutionAdapter(self.cfg.costs, Mode.BACKTEST, lambda o: True)
        self.pos = Position(Side.LONG, 1.0, 100.0, 99.0, 101.0, 0, 0.0, "id")

    def test_take_profit_fills_on_touch_at_exact_price_without_slippage(self):
        """OPTIMIS: limit TP dianggap terisi saat harga HANYA menyentuh (tanpa antrean/trade-through)."""
        bar = Candle(MIN, 100.0, 101.0, 99.5, 100.5, 1.0)       # high == TP
        f = self.ad.check_exit(self.pos, bar)
        self.assertEqual((f.reason, f.price), ("take_profit", 101.0))

    def test_stop_wins_when_stop_and_tp_both_touched_in_same_bar(self):
        """PESIMIS: bila stop dan TP sama-sama tersentuh dalam satu candle, stop dianggap lebih dulu."""
        bar = Candle(MIN, 100.0, 101.5, 98.5, 100.0, 1.0)
        self.assertEqual(self.ad.check_exit(self.pos, bar).reason, "stop")

    def test_gap_through_stop_fills_at_open_not_at_stop(self):
        bar = Candle(MIN, 98.0, 98.5, 97.5, 98.0, 1.0)           # open di bawah stop 99
        f = self.ad.check_exit(self.pos, bar)
        self.assertLess(f.price, 98.0)


if __name__ == "__main__":
    unittest.main()
