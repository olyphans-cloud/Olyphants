"""v0.2.0 - Safeguard TERHUBUNG ke engine: spread order book, batas waktu posisi, trailing stop,
filter edge-vs-biaya, batas entry per jam. Skenario candle buatan: hasil dihitung manual."""
import http.server
import json
import threading
import unittest
from dataclasses import replace

from btc_robot.config import ConfigError, Mode, validate
from btc_robot.data import DataError, fetch_book_ticker
from btc_robot.execution import ExecutionRejected, SimulatedExecutionAdapter
from btc_robot.models import BookQuote

from tests._v020_helpers import HOUR, MIN, base_cfg, flat_bars, make_engine, run_all, with_bar


def cfg_with(interval="1h", **risk):
    return base_cfg(interval, **risk)


class MaxHoldTests(unittest.TestCase):
    def test_position_force_closed_at_max_hold(self):
        cfg = cfg_with(max_hold_sec=5 * 3600)
        bars = flat_bars(20, HOUR)
        eng = make_engine(cfg, {2: (2.0, 10.0)}, bars)
        run_all(eng, bars)
        # sinyal di bar 2 -> entry di OPEN bar 3 -> tertahan 5 jam penuh di CLOSE bar 7
        self.assertEqual(eng.counters["entries"], 1)
        self.assertEqual(eng.counters["exits_max_hold"], 1)
        t = eng.ledger.trades[0]
        self.assertEqual(t["exit_reason"], "max_hold")
        self.assertEqual(t["entry_ts"], bars[3].ts)
        self.assertEqual(t["exit_ts"], bars[7].ts)
        self.assertIsNone(eng.ledger.position)

    def test_not_closed_before_limit(self):
        cfg = cfg_with(max_hold_sec=5 * 3600)
        bars = flat_bars(7, HOUR)  # data berakhir di bar 6: 4 jam tertahan < 5 jam
        eng = make_engine(cfg, {2: (2.0, 10.0)}, bars)
        for i in range(len(bars)):
            eng.process_bar(bars, i)
        self.assertEqual(eng.counters["exits_max_hold"], 0)
        self.assertIsNotNone(eng.ledger.position)

    def test_disabled_by_default(self):
        bars = flat_bars(60, HOUR)
        eng = make_engine(cfg_with(), {2: (2.0, 10.0)}, bars)
        for i in range(len(bars)):
            eng.process_bar(bars, i)
        self.assertEqual(eng.counters["exits_max_hold"], 0)
        self.assertIsNotNone(eng.ledger.position)

    def test_stop_has_priority_over_max_hold_on_same_bar(self):
        cfg = cfg_with(max_hold_sec=5 * 3600)
        bars = flat_bars(20, HOUR)
        with_bar(bars, 7, 100, 100.05, 97.0, 99.0)  # menembus stop 98 tepat di bar batas waktu
        eng = make_engine(cfg, {2: (2.0, 10.0)}, bars)
        run_all(eng, bars)
        self.assertEqual(eng.ledger.trades[0]["exit_reason"], "stop")


class TrailingStopTests(unittest.TestCase):
    def _rising(self):
        bars = flat_bars(14, HOUR)
        px = 100.0
        for i in range(3, 10):  # naik 1% per candle (open = close sebelumnya)
            o, c = px, px * 1.01
            with_bar(bars, i, o, c * 1.001, o * 0.9995, c)
            px = c
        for i in range(10, 14):
            with_bar(bars, i, px, px * 1.0005, px * 0.9995, px)
        return bars, px

    def test_stop_ratchets_up_and_locks_profit(self):
        cfg = cfg_with(trailing_trail_pct=1.0, trailing_activation_pct=0.5)
        bars, top = self._rising()
        with_bar(bars, 12, top, top, top * 0.95, top * 0.96)  # jatuh 5%: kena trailing stop
        eng = make_engine(cfg, {2: (2.0, 50.0)}, bars)
        stops = []
        for i in range(len(bars)):
            eng.process_bar(bars, i)
            if eng.ledger.position is not None:
                stops.append(eng.ledger.position.stop_price)
        eng.finalize(bars)
        self.assertGreater(eng.counters["trailing_updates"], 3)
        self.assertTrue(all(b >= a for a, b in zip(stops, stops[1:])), "stop tidak boleh turun")
        t = eng.ledger.trades[0]
        self.assertEqual(t["exit_reason"], "stop")
        self.assertGreater(t["exit_price"], t["entry_price"], "stop awal 98 sudah tergeser di atas entry")
        self.assertGreater(t["net_pnl"], 0)

    def test_stop_never_loosens_on_pullback(self):
        cfg = cfg_with(trailing_trail_pct=1.0, trailing_activation_pct=0.5)
        bars, top = self._rising()
        for i in (10, 11):  # pullback 0.6% dari puncak: masih di atas stop, tapi calon stop baru lebih rendah
            with_bar(bars, i, top * 0.997, top * 0.998, top * 0.993, top * 0.994)
        eng = make_engine(cfg, {2: (2.0, 50.0)}, bars)
        for i in range(12):
            eng.process_bar(bars, i)
        self.assertIsNotNone(eng.ledger.position)
        self.assertAlmostEqual(eng.ledger.position.stop_price, top * 0.99, places=6)

    def test_without_trailing_same_path_loses_back_to_original_stop(self):
        cfg = cfg_with()  # trailing nonaktif
        bars, top = self._rising()
        with_bar(bars, 12, top, top, top * 0.80, top * 0.82)
        eng = make_engine(cfg, {2: (2.0, 50.0)}, bars)
        run_all(eng, bars)
        t = eng.ledger.trades[0]
        self.assertEqual(eng.counters["trailing_updates"], 0)
        self.assertAlmostEqual(t["exit_price"] / 98.0, 1.0, delta=0.01)  # keluar di sekitar stop awal 98

    def test_trailing_uses_closed_bar_no_lookahead(self):
        """Stop yang digeser di CLOSE bar k hanya berlaku mulai bar k+1 (bukan bar k itu sendiri)."""
        cfg = cfg_with(trailing_trail_pct=1.0, trailing_activation_pct=0.5)
        bars = flat_bars(8, HOUR)
        with_bar(bars, 3, 100, 100.2, 99.9, 100.1)
        with_bar(bars, 4, 100.1, 103.0, 99.99, 102.0)   # close 102 -> stop baru 100.98; low 99.99 < 100.98
        eng = make_engine(cfg, {2: (2.0, 50.0)}, bars)
        for i in range(5):
            eng.process_bar(bars, i)
        self.assertIsNotNone(eng.ledger.position, "low bar 4 tidak boleh memicu stop yang baru dihitung di close bar 4")
        self.assertAlmostEqual(eng.ledger.position.stop_price, 102.0 * 0.99, places=6)


class MinEdgeTests(unittest.TestCase):
    def test_tp_below_cost_multiple_rejected(self):
        cfg = cfg_with(min_edge_cost_mult=2.0)  # biaya pulang-pergi default ~0.28% -> TP minimal ~0.56%
        bars = flat_bars(10, HOUR)
        eng = make_engine(cfg, {2: (1.0, 0.3)}, bars)
        run_all(eng, bars)
        self.assertEqual(eng.counters["entries"], 0)
        self.assertEqual(eng.counters["risk_reject:edge_tidak_cukup_untuk_biaya"], 1)

    def test_tp_above_cost_multiple_accepted(self):
        cfg = cfg_with(min_edge_cost_mult=2.0)
        bars = flat_bars(10, HOUR)
        eng = make_engine(cfg, {2: (1.0, 1.0)}, bars)
        run_all(eng, bars)
        self.assertEqual(eng.counters["entries"], 1)


class EntriesPerHourTests(unittest.TestCase):
    def _bars(self):
        bars = flat_bars(30, MIN)
        for i in (3, 13):
            with_bar(bars, i, 100, 100.6, 99.95, 100.3)  # TP 100.5 tercapai pada candle entry
        return bars

    def test_default_one_entry_per_hour(self):
        bars = self._bars()
        eng = make_engine(cfg_with("1m"), {2: (1.0, 0.5), 12: (1.0, 0.5)}, bars)
        run_all(eng, bars)
        self.assertEqual(eng.counters["entries"], 1)
        self.assertEqual(eng.counters["entry_hour_limit"], 1)

    def test_two_entries_when_allowed(self):
        bars = self._bars()
        eng = make_engine(cfg_with("1m", max_entries_per_hour=2), {2: (1.0, 0.5), 12: (1.0, 0.5)}, bars)
        run_all(eng, bars)
        self.assertEqual(eng.counters["entries"], 2)
        self.assertEqual(eng.counters["entry_hour_limit"], 0)


class SpreadGateTests(unittest.TestCase):
    NOW = 1_000.0

    def _run(self, provider, mode=Mode.PAPER, require=True):
        cfg = cfg_with(require_spread_check=require)
        bars = flat_bars(8, HOUR)
        eng = make_engine(cfg, {2: (2.0, 10.0)}, bars, mode=mode, spread_provider=provider)
        for i in range(len(bars)):
            eng.process_bar(bars, i)
        return eng

    def _spread_rejects(self, eng):
        return {k for k in eng.counters if k.startswith("safeguard_reject:spread:")}

    def test_tight_spread_allows_entry(self):
        eng = self._run(lambda bar: BookQuote(99.99, 100.01, self.NOW, self.NOW))
        self.assertEqual(eng.counters["entries"], 1)

    def test_wide_spread_blocks_entry(self):
        eng = self._run(lambda bar: BookQuote(99.0, 101.0, self.NOW, self.NOW))
        self.assertEqual(eng.counters["entries"], 0)
        self.assertEqual(self._spread_rejects(eng), {"safeguard_reject:spread:spread_limit"})

    def test_stale_orderbook_blocks_entry(self):
        eng = self._run(lambda bar: BookQuote(99.99, 100.01, self.NOW - 60, self.NOW))
        self.assertEqual(eng.counters["entries"], 0)
        self.assertEqual(self._spread_rejects(eng), {"safeguard_reject:spread:orderbook_stale_or_future"})

    def test_no_provider_fails_closed(self):
        eng = self._run(None)
        self.assertEqual(eng.counters["entries"], 0)
        self.assertEqual(self._spread_rejects(eng), {"safeguard_reject:spread:spread_unavailable"})

    def test_provider_returning_none_fails_closed(self):
        eng = self._run(lambda bar: None)
        self.assertEqual(eng.counters["entries"], 0)

    def test_provider_exception_fails_closed(self):
        def boom(bar):
            raise ConnectionError("putus")
        eng = self._run(boom)
        self.assertEqual(eng.counters["entries"], 0)
        self.assertEqual(self._spread_rejects(eng), {"safeguard_reject:spread:spread_unavailable"})

    def test_crossed_book_blocks_entry(self):
        eng = self._run(lambda bar: BookQuote(101.0, 99.0, self.NOW, self.NOW))
        self.assertEqual(eng.counters["entries"], 0)

    def test_backtest_ignores_flag_because_it_has_no_orderbook(self):
        eng = self._run(None, mode=Mode.BACKTEST)
        self.assertEqual(eng.counters["entries"], 1)

    def test_check_disabled_by_default_in_paper(self):
        eng = self._run(None, require=False)
        self.assertEqual(eng.counters["entries"], 1)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if "BAD" in self.path:
            body, code = b"<<bukan json>>", 200
        elif "ERR" in self.path:
            body, code = b"{}", 500
        else:
            body, code = json.dumps({"symbol": "BTCUSDT", "bidPrice": "100.00", "bidQty": "1",
                                     "askPrice": "100.02", "askQty": "1"}).encode(), 200
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BookTickerFetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_parses_quote(self):
        q = fetch_book_ticker("BTCUSDT", base_url=self.base)
        self.assertEqual((q.bid, q.ask), (100.0, 100.02))
        self.assertLess(abs(q.now - q.observed_at), 1.0)

    def test_invalid_json_raises_dataerror(self):
        with self.assertRaises(DataError):
            fetch_book_ticker("BAD", base_url=self.base)

    def test_http_error_raises_dataerror(self):
        with self.assertRaises(DataError):
            fetch_book_ticker("ERR", base_url=self.base)

    def test_connection_refused_raises_dataerror(self):
        with self.assertRaises(DataError):
            fetch_book_ticker("BTCUSDT", base_url="http://127.0.0.1:1", timeout=1.0)


class ConfigValidationTests(unittest.TestCase):
    def test_invalid_values_rejected(self):
        for bad in ({"max_entries_per_hour": 0}, {"max_hold_sec": 10}, {"trailing_trail_pct": 25.0},
                    {"max_spread_pct": 0.0}, {"min_edge_cost_mult": -1.0}, {"max_orderbook_age_sec": 0.0}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                base_cfg("1h", **bad)


if __name__ == "__main__":
    unittest.main()
