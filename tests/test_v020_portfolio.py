"""v0.2.0 - Scanner multi-koin, risiko bersama lintas sleeve, simulasi kausal H1+M1."""
import unittest
from dataclasses import replace
from unittest import mock

from btc_robot import portfolio as P
from btc_robot.backtest import run_backtest
from btc_robot.config import load_config, validate
from btc_robot.data import generate_synthetic
from btc_robot.models import BookQuote, Candle
from btc_robot.portfolio import (PortfolioLimits, PortfolioRiskGate, ScanConfig, SleeveSpec,
                                 run_portfolio_backtest, scan)

from tests._v020_helpers import HOUR, StubStrategy, base_cfg, flat_bars, make_engine, with_bar

H1_CFG, M1_CFG = "config.h1.example.json", "config.m1.example.json"


def mk_series(n, itv, price=100.0, vol=1000.0, wick=0.3, end_ts=None):
    start = (end_ts - n * itv) if end_ts is not None else 1_700_000_000_000
    return [Candle(start + i * itv, price, price + wick, price - wick, price, vol) for i in range(n)]


# ------------------------------------------------------------------ Scanner
class ScannerTests(unittest.TestCase):
    def setUp(self):
        end = 1_700_000_000_000
        self.data = {
            "AAA": mk_series(200, HOUR, 100, vol=5000, wick=1.0, end_ts=end),
            "BBB": mk_series(200, HOUR, 100, vol=3000, wick=0.8, end_ts=end),
            "CCC": mk_series(200, HOUR, 100, vol=100, wick=0.8, end_ts=end),      # likuiditas rendah
            "DDD": mk_series(200, HOUR, 100, vol=4000, wick=0.001, end_ts=end),   # volatilitas ~0
            "OLD": mk_series(200, HOUR, 100, vol=4000, wick=1.0, end_ts=end - 50 * HOUR),  # data basi
            "NEW": mk_series(20, HOUR, 100, vol=4000, wick=1.0, end_ts=end),      # data kurang
        }

    def _by(self, res):
        return {r.symbol: r for r in res}

    def test_auto_ranks_by_liquidity_and_volatility_and_applies_filters(self):
        cfg = ScanConfig(mode="auto", top_n=2, min_avg_quote_volume=50_000, min_atr_pct=0.1)
        r = self._by(scan(self.data, cfg))
        self.assertTrue(r["AAA"].selected and r["BBB"].selected)
        self.assertIn("likuiditas_rendah", r["CCC"].reasons)
        self.assertIn("volatilitas_rendah", r["DDD"].reasons)
        self.assertIn("data_basi", r["OLD"].reasons)
        self.assertEqual(r["NEW"].reasons, ("data_kurang",))
        self.assertGreater(r["AAA"].score, r["BBB"].score)

    def test_top_n_limits_selection(self):
        res = scan(self.data, ScanConfig(mode="auto", top_n=1, min_avg_quote_volume=50_000, min_atr_pct=0.1))
        self.assertEqual([x.symbol for x in res if x.selected], ["AAA"])

    def test_manual_whitelist_only_and_filters_still_apply(self):
        cfg = ScanConfig(mode="manual", symbols=("BBB", "CCC", "ZZZ"), min_avg_quote_volume=50_000)
        r = self._by(scan(self.data, cfg))
        self.assertEqual(set(r), {"BBB", "CCC", "ZZZ"})          # AAA tidak masuk daftar
        self.assertTrue(r["BBB"].selected)
        self.assertIn("likuiditas_rendah", r["CCC"].reasons)
        self.assertEqual(r["ZZZ"].reasons, ("data_tidak_ada",))

    def test_manual_without_symbols_rejected(self):
        with self.assertRaises(ValueError):
            scan(self.data, ScanConfig(mode="manual"))

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            scan(self.data, ScanConfig(mode="acak"))

    def test_spread_filter_requires_known_quote(self):
        cfg = ScanConfig(mode="auto", top_n=5, max_spread_pct=0.1, min_avg_quote_volume=50_000, min_atr_pct=0.1)
        q = {"AAA": BookQuote(99.99, 100.01, 1.0, 1.0), "BBB": BookQuote(99.0, 101.0, 1.0, 1.0)}
        r = self._by(scan(self.data, cfg, quotes=q))
        self.assertTrue(r["AAA"].selected)
        self.assertIn("spread_lebar", r["BBB"].reasons)
        self.assertIn("spread_tidak_diketahui", r["CCC"].reasons)

    def test_deterministic(self):
        cfg = ScanConfig(mode="auto", top_n=3)
        self.assertEqual(scan(self.data, cfg), scan(self.data, cfg))


# ------------------------------------------------------------------ Gerbang risiko bersama (strategi stub)
def stub_patch(plans):
    """make_strategy dipatch agar tiap sleeve (berurutan) memakai StubStrategy dengan plan-nya."""
    it = iter(plans)
    return mock.patch.object(P, "make_strategy", side_effect=lambda cfg: StubStrategy(next(it)))


def spec(name, symbol, bars, **risk):
    return SleeveSpec(name, replace(base_cfg("1h", **risk), symbol=symbol), bars)


class SharedRiskTests(unittest.TestCase):
    def setUp(self):
        self.flat = flat_bars(60, HOUR)

    def _run(self, plans, specs, limits):
        with stub_patch(plans):
            return run_portfolio_backtest(specs, limits)

    def test_max_open_positions_across_sleeves(self):
        res = self._run([{2: (1.0, 50.0)}, {2: (1.0, 50.0)}],
                        [spec("A", "AAAUSDT", self.flat), spec("B", "BBBUSDT", self.flat)],
                        PortfolioLimits(max_open_positions=1))
        self.assertEqual(res.sleeves["A"]["counters"].get("entries", 0), 1)
        self.assertEqual(res.sleeves["B"]["counters"].get("entries", 0), 0)
        self.assertEqual(res.sleeves["B"]["counters"]["portfolio_reject:max_open_positions"], 1)

    def test_one_position_per_symbol_blocks_second_mode_on_same_symbol(self):
        res = self._run([{2: (1.0, 50.0)}, {2: (1.0, 50.0)}],
                        [spec("H1-BTC", "BTCUSDT", self.flat), spec("M1-BTC", "BTCUSDT", self.flat)],
                        PortfolioLimits(max_open_positions=2, max_positions_per_symbol=1))
        self.assertEqual(res.sleeves["M1-BTC"]["counters"]["portfolio_reject:symbol_position_limit"], 1)
        self.assertEqual(res.sleeves["M1-BTC"]["counters"].get("entries", 0), 0)

    def test_two_positions_allowed_on_different_symbols(self):
        res = self._run([{2: (1.0, 50.0)}, {2: (1.0, 50.0)}],
                        [spec("A", "AAAUSDT", self.flat), spec("B", "BBBUSDT", self.flat)],
                        PortfolioLimits(max_open_positions=2))
        self.assertEqual(res.gate_counters, {"ok": 2})

    def test_total_exposure_limit(self):
        def specs():
            return [spec("A", "AAAUSDT", self.flat), spec("B", "BBBUSDT", self.flat)]
        tight = self._run([{2: (1.0, 50.0)}, {2: (1.0, 50.0)}], specs(),
                          PortfolioLimits(max_open_positions=2, max_total_exposure_pct=10.0))
        self.assertEqual(tight.gate_counters.get("max_total_exposure"), 2)
        loose = self._run([{2: (1.0, 50.0)}, {2: (1.0, 50.0)}], specs(),
                          PortfolioLimits(max_open_positions=2, max_total_exposure_pct=100.0))
        self.assertEqual(loose.gate_counters.get("ok"), 2)

    def _loss_on_day1_then_signals(self, limits):
        a = flat_bars(60, HOUR)
        with_bar(a, 3, 100, 100.05, 98.5, 98.8)           # stop 1% (99.0) kena di candle entry A -> rugi
        b = flat_bars(60, HOUR)
        return self._run([{2: (1.0, 50.0)}, {6: (1.0, 50.0), 30: (1.0, 50.0)}],
                         [spec("A", "AAAUSDT", a), spec("B", "BBBUSDT", b)], limits)

    def test_daily_loss_blocks_same_day_and_resets_next_utc_day(self):
        res = self._loss_on_day1_then_signals(
            PortfolioLimits(max_open_positions=2, max_daily_loss_pct=0.2, max_drawdown_pct=50.0))
        self.assertEqual(res.gate_counters.get("portfolio_daily_loss"), 1)   # sinyal bar 6 (hari 1) ditolak
        self.assertEqual(res.sleeves["B"]["counters"].get("entries", 0), 1)  # sinyal bar 30 (hari 2) lolos
        self.assertFalse(res.metrics["portfolio_halted"])

    def test_portfolio_drawdown_latches_halt(self):
        res = self._loss_on_day1_then_signals(
            PortfolioLimits(max_open_positions=2, max_daily_loss_pct=20.0, max_drawdown_pct=0.2))
        self.assertTrue(res.metrics["portfolio_halted"])
        self.assertEqual(res.gate_counters.get("portfolio_halt"), 2)          # bar 6 dan bar 30 ditolak
        self.assertEqual(res.sleeves["B"]["counters"].get("entries", 0), 0)

    def test_gate_error_fails_closed_in_engine(self):
        bars = flat_bars(10, HOUR)
        gate = PortfolioRiskGate(PortfolioLimits())  # sleeve tidak terdaftar -> KeyError di gerbang
        eng = make_engine(base_cfg("1h"), {2: (1.0, 50.0)}, bars, entry_gate=lambda o, b: gate.check("hantu", o, b))
        for i in range(len(bars)):
            eng.process_bar(bars, i)
        self.assertEqual(eng.counters["entries"], 0)
        self.assertEqual(eng.counters["portfolio_reject:gate_error:KeyError"], 1)

    def test_gate_only_returns_verdict(self):
        """Gerbang hanya mengembalikan (ok, alasan); tidak punya jalur mengubah qty/harga order."""
        import inspect
        self.assertEqual(list(inspect.signature(PortfolioRiskGate.check).parameters), ["self", "sleeve", "order", "bar"])

    def test_limits_validation(self):
        for bad in ({"max_total_exposure_pct": 150.0}, {"max_open_positions": 0}, {"max_drawdown_pct": 0.0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PortfolioLimits(**bad)

    def test_duplicate_sleeve_names_rejected(self):
        with self.assertRaises(ValueError):
            run_portfolio_backtest([spec("A", "AAAUSDT", self.flat), spec("A", "BBBUSDT", self.flat)])


# ------------------------------------------------------------------ Simulasi dengan strategi sungguhan
def _overlaps(trades_a, trades_b):
    """Jangkauan konservatif: posisi dianggap terbuka sampai CLOSE candle keluar (state gerbang baru berubah di sana)."""
    def span(t):
        return t["entry_ts"], t["exit_ts"] + t["interval_ms"]
    n = 0
    for a in trades_a:
        for b in trades_b:
            (a0, a1), (b0, b1) = span(a), span(b)
            if a0 < b1 and b0 < a1:
                n += 1
    return n


class PortfolioSimulationTests(unittest.TestCase):
    def test_single_sleeve_equals_standalone_backtest(self):
        """Urutan event begin/end kausal harus menghasilkan hasil IDENTIK dengan run_backtest."""
        cfg = validate(load_config(H1_CFG))
        c = generate_synthetic(3000, 1, HOUR)
        solo = run_backtest(c, cfg)
        pr = run_portfolio_backtest([SleeveSpec("H1", cfg, c)], PortfolioLimits(max_total_exposure_pct=100.0))
        self.assertAlmostEqual(pr.metrics["final_equity"], solo.metrics["final_equity"], places=8)
        self.assertEqual(pr.sleeves["H1"]["metrics"]["trades"], solo.metrics["trades"])

    def _h1_m1_same_symbol(self):
        h1, m1 = validate(load_config(H1_CFG)), validate(load_config(M1_CFG))
        start = 1_700_000_000_000 // HOUR * HOUR
        # seed 1 / 800 jam dipilih karena tanpa batas per-simbol menghasilkan tumpang tindih (lihat tes kontrol)
        return [SleeveSpec("H1-BTC", h1, generate_synthetic(800, 1, HOUR, start_ts=start)),
                SleeveSpec("M1-BTC", m1, generate_synthetic(800 * 60, 1, 60_000, start_ts=start, vol=0.0007))]

    def _split(self, res):
        return ([t for t in res.trades if t["sleeve"] == "H1-BTC"], [t for t in res.trades if t["sleeve"] == "M1-BTC"])

    def test_h1_and_m1_on_same_symbol_never_hold_overlapping_positions(self):
        res = run_portfolio_backtest(self._h1_m1_same_symbol(),
                                     PortfolioLimits(max_open_positions=2, max_positions_per_symbol=1))
        h1, m1 = self._split(res)
        self.assertGreater(len(h1), 0)
        self.assertGreater(len(m1), 0)
        self.assertEqual(_overlaps(h1, m1), 0)
        self.assertGreater(res.gate_counters.get("symbol_position_limit", 0), 0, "batas harus benar-benar aktif menolak")

    def test_control_without_symbol_limit_the_same_data_does_overlap(self):
        """Kontrol: tanpa batas per-simbol data yang sama MENUMPUK -> pemeriksaan tumpang tindih bermakna."""
        res = run_portfolio_backtest(self._h1_m1_same_symbol(),
                                     PortfolioLimits(max_open_positions=2, max_positions_per_symbol=2))
        h1, m1 = self._split(res)
        self.assertGreater(_overlaps(h1, m1), 0)

    def test_deterministic(self):
        a = run_portfolio_backtest(self._h1_m1_same_symbol())
        b = run_portfolio_backtest(self._h1_m1_same_symbol())
        self.assertEqual(a.result_hash, b.result_hash)

    def test_scanner_integration_skips_unselected_symbols(self):
        h1 = validate(load_config(H1_CFG))
        c1 = generate_synthetic(800, 1, HOUR)
        thin = [Candle(x.ts, x.open, x.high, x.low, x.close, 0.001) for x in generate_synthetic(800, 2, HOUR)]
        res = run_portfolio_backtest(
            [SleeveSpec("A", replace(h1, symbol="AAAUSDT"), c1), SleeveSpec("B", replace(h1, symbol="BBBUSDT"), thin)],
            PortfolioLimits(), ScanConfig(mode="auto", top_n=2, min_avg_quote_volume=1000.0))
        self.assertEqual(res.skipped, ["B"])
        self.assertNotIn("B", res.sleeves)

    def test_scanner_selecting_nothing_is_an_error(self):
        h1 = validate(load_config(H1_CFG))
        with self.assertRaises(ValueError):
            run_portfolio_backtest([SleeveSpec("A", h1, generate_synthetic(400, 1, HOUR))],
                                   PortfolioLimits(), ScanConfig(mode="auto", min_avg_quote_volume=1e18))


if __name__ == "__main__":
    unittest.main()
