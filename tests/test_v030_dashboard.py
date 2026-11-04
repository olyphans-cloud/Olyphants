"""v0.3.0 - Dashboard: keamanan HTTP, ACC tidak membuka order, pemisahan M1/H1, HALT/kill switch/identitas."""
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from btc_robot import dashboard as D
from btc_robot.data import generate_synthetic
from btc_robot.models import BookQuote
from btc_robot.portfolio import ScanConfig
from btc_robot.safeguard import SafeguardExecutionEngine
from btc_robot.identity import write_identity
from dataclasses import replace

H, M = 3_600_000, 60_000
COINS = ["BTCUSDT", "ETHUSDT"]


class Feed:
    """Sumber candle demo deterministik: jendela 500 candle berakhir di indeks 'k' milik tiap (koin, interval)."""
    def __init__(self):
        self.data = {}
        for i, c in enumerate(COINS):
            self.data[(c, "1h")] = generate_synthetic(1200, 3 + i, H, start_price=30000 / (i + 1))
            self.data[(c, "1m")] = generate_synthetic(4000, 3 + i, M, vol=0.0007, start_price=30000 / (i + 1))
        self.k = {("BTCUSDT", "1h"): 300, ("ETHUSDT", "1h"): 300, ("BTCUSDT", "1m"): 700, ("ETHUSDT", "1m"): 700}
        self.cur = None

    def source(self, symbol, interval):
        self.cur = (symbol, interval)
        d, k = self.data[self.cur], self.k[self.cur]
        return d[max(0, k - 499):k + 1]

    def advance(self, symbol, interval, n=1):
        self.k[(symbol, interval)] += n

    def now_ms(self):
        sym, itv = self.cur
        c = self.data[self.cur][self.k[self.cur]]
        return c.ts + (H if itv == "1h" else M) + 15_000


def make_dash(tmp, feed=None, **kw):
    feed = feed or Feed()
    dash = D.Dashboard(tmp, ".", COINS, data_source=feed.source,
                       spread_provider=lambda bar: BookQuote(100.0, 100.003, 1.0, 1.0),
                       now_ms=lambda: feed.now_ms(), token="tok-test", **kw)
    return dash, feed


def run_steps(dash, feed, tf, n):
    sym = dash.coin[tf]
    itv = "1h" if tf == "H1" else "1m"
    for _ in range(n):
        feed.advance(sym, itv)
        feed.source(sym, itv)
        dash.step(tf)


class DashBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.dash, self.feed = make_dash(self.tmp)

    def tearDown(self):
        self._td.cleanup()

    def arm(self, tf, coin="BTCUSDT"):
        self.dash.select("MANUAL", tf, coin)
        r = self.dash.acc(tf)
        self.assertTrue(r["armed"], r)
        return r


class HttpSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.dash, cls.feed = make_dash(cls._td.name)
        cls.srv = D.serve(cls.dash, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls._td.cleanup()

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if host:
            h["Host"] = host
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r

    def post(self, path, obj, token="tok-test", ctype="application/json", raw=None):
        h = {"Content-Type": ctype}
        if token is not None:
            h["X-Dashboard-Token"] = token
        body = raw if raw is not None else json.dumps(obj).encode()
        h["Content-Length"] = str(len(body))
        return self.req("POST", path, body, h)

    def test_refuses_to_bind_to_non_loopback(self):
        with self.assertRaises(ValueError):
            D.serve(self.dash, "0.0.0.0", 0)

    def test_index_is_served_with_security_headers_and_required_controls(self):
        st, body, r = self.req("GET", "/")
        html = body.decode()
        self.assertEqual(st, 200)
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        for needle in ("AI Scan", "Manual", "ACC", "TANPA UANG ASLI", "M15", "H1", "Saldo demo", "Jumlah transaksi",
                       "Win rate", "Profit", "Loss", "PnL bersih", "Drawdown", "Posisi aktif", "Safe Guard", "HALT",
                       "Alasan order ditolak", "Status robot", 'id="scan-ai"', 'id="scan-manual"'):
            self.assertIn(needle, html)
        self.assertNotIn("innerHTML", html, "render harus lewat textContent")
        self.assertIn('content="tok-test"', html)

    def test_state_endpoint_reports_paper_and_no_real_money(self):
        st, body, _ = self.req("GET", "/api/state")
        s = json.loads(body)
        self.assertEqual((st, s["mode"], s["real_money"]), (200, "PAPER", False))
        self.assertEqual(set(s["timeframes"]), {"M15", "H1"})

    def test_foreign_host_header_is_rejected(self):
        self.assertEqual(self.req("GET", "/api/state", host="evil.example.com")[0], 403)
        self.assertEqual(self.post("/api/acc", {"timeframe": "M15"})[0], 200)          # kontrol: host normal diterima
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/acc", body=b"{}", headers={"Host": "evil.example.com", "Content-Type": "application/json",
                                                           "X-Dashboard-Token": "tok-test", "Content-Length": "2"})
        self.assertEqual(c.getresponse().status, 403)

    def test_post_requires_correct_token(self):
        self.assertEqual(self.post("/api/acc", {"timeframe": "M15"}, token=None)[0], 403)
        self.assertEqual(self.post("/api/acc", {"timeframe": "M15"}, token="salah")[0], 403)

    def test_post_validates_content_type_size_and_json(self):
        self.assertEqual(self.post("/api/acc", {}, ctype="text/plain")[0], 415)
        self.assertEqual(self.post("/api/acc", None, raw=b"x" * 5000)[0], 413)
        self.assertEqual(self.post("/api/acc", None, raw=b"{bukan json")[0], 400)
        self.assertEqual(self.post("/api/acc", None, raw=b"[1,2]")[0], 400)

    def test_no_order_endpoint_exists(self):
        for p in ("/api/order", "/api/buy", "/api/sell", "/api/live"):
            self.assertEqual(self.post(p, {"timeframe": "M15"})[0], 404, p)

    def test_select_validation_over_http(self):
        self.assertEqual(self.post("/api/select", {"scan_mode": "MANUAL", "timeframe": "M5", "coin": "BTCUSDT"})[0], 400)
        self.assertEqual(self.post("/api/select", {"scan_mode": "MANUAL", "timeframe": "M15", "coin": "DOGE; rm -rf"})[0], 400)
        self.assertEqual(self.post("/api/select", {"scan_mode": "ACAK", "timeframe": "M15", "coin": "BTCUSDT"})[0], 400)
        st, body, _ = self.post("/api/select", {"scan_mode": "MANUAL", "timeframe": "M15", "coin": "BTCUSDT"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["timeframes"]["M15"]["coin"], "BTCUSDT")


@unittest.skipUnless(shutil.which("node"), "node tidak tersedia")
class DashboardScriptTests(unittest.TestCase):
    """Menjalankan script halaman di Node (DOM tiruan) terhadap server nyata. BUKAN uji visual di browser."""
    def test_page_script_runs_without_errors_and_buttons_drive_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            dash, feed = make_dash(tmp)
            srv = D.serve(dash, "127.0.0.1", 0)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                p = subprocess.run(["node", os.path.join("tests", "js", "dashboard_dom_check.js"),
                                    f"http://127.0.0.1:{srv.server_address[1]}"], capture_output=True, text=True, timeout=60)
            finally:
                srv.shutdown()
                srv.server_close()
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(out["errors"], [], p.stderr[-300:])
        self.assertEqual(out["missing"], [])
        self.assertEqual([s[1] for s in out["steps"]], ["OK", "OK", "OK"])
        self.assertEqual(out["status"]["M15"], "BELUM_DIPILIH")
        self.assertEqual(out["status"]["H1"], "AKTIF")


class AccSemanticsTests(DashBase):
    def test_acc_without_selection_is_refused(self):
        r = self.dash.acc("M15")
        self.assertFalse(r["armed"])
        self.assertEqual(r["reason"], "pilihan_lengkap")
        self.assertEqual(r["orders_opened"], 0)

    def test_acc_never_opens_orders_or_runs_the_cycle(self):
        with mock.patch.object(D, "paper_step", side_effect=AssertionError("ACC tidak boleh menjalankan siklus")):
            r = self.arm("H1")
        self.assertEqual(r["orders_opened"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.dash.sleeve_cfg("H1", "BTCUSDT").state_dir, "paper_state.json")))
        s = self.dash.stats("H1")
        self.assertEqual((s["trades"], s["position"]), (0, None))

    def test_acc_refused_when_halted(self):
        self.arm("H1")
        run_steps(self.dash, self.feed, "H1", 3)
        cfg = self.dash.sleeve_cfg("H1", "BTCUSDT")
        SafeguardExecutionEngine(state_path=os.path.join(cfg.state_dir, "safeguard_state.json")).halt("uji")
        r = self.dash.acc("H1")
        self.assertFalse(r["armed"])
        self.assertEqual(r["reason"], "tidak_halt")
        self.assertEqual(self.dash.stats("H1")["robot_status"], "HALT")

    def test_acc_refused_by_shared_kill_switch(self):
        self.dash.select("MANUAL", "M15", "BTCUSDT")
        os.makedirs(os.path.join(self.tmp, "state"), exist_ok=True)
        open(os.path.join(self.tmp, "state", "KILL_ALL"), "w").close()
        r = self.dash.acc("M15")
        self.assertFalse(r["armed"])
        self.assertEqual(r["reason"], "kill_switch_mati")

    def test_acc_refused_on_identity_mismatch(self):
        self.dash.select("MANUAL", "M15", "BTCUSDT")
        cfg = self.dash.sleeve_cfg("M15", "BTCUSDT")
        write_identity(replace(cfg, strategy_mode="h1_trend", interval="1h"))      # direktori milik mode lain
        r = self.dash.acc("M15")
        self.assertFalse(r["armed"])
        self.assertEqual(r["reason"], "identitas_state")

    def test_manual_selection_then_step_works_without_a_prior_scan(self):
        """Regresi: step memanggil now_ms() sebelum data diambil -> TypeError (500) pada DemoFeed tanpa scan."""
        dash, feed = make_dash(self.tmp + "/fresh")
        dash.select("MANUAL", "H1", "BTCUSDT")
        self.assertTrue(dash.acc("H1")["armed"])
        feed.cur = None                                  # tidak ada pengambilan data sebelumnya
        out = dash.step("H1")
        self.assertEqual(out["result"]["status"], "ok")

    def test_data_source_failure_is_reported_as_no_data_not_500(self):
        dash, feed = make_dash(self.tmp + "/nodata")
        dash.select("MANUAL", "H1", "BTCUSDT")
        dash.acc("H1")
        dash.data_source = lambda s, i: (_ for _ in ()).throw(ConnectionError("putus"))
        out = dash.step("H1")
        self.assertEqual(out["result"]["status"], "no_data")

    def test_step_requires_acc(self):
        self.dash.select("MANUAL", "H1", "BTCUSDT")
        with self.assertRaises(D.DashboardError) as cm:
            self.dash.step("H1")
        self.assertEqual(cm.exception.status, 409)

    def test_kill_switch_after_acc_still_blocks_orders_in_pipeline(self):
        """ACC bukan izin menembus keselamatan: kill switch yang muncul setelah ACC tetap memblok entry."""
        self.arm("H1")
        os.makedirs(os.path.join(self.tmp, "state"), exist_ok=True)
        open(os.path.join(self.tmp, "state", "KILL_ALL"), "w").close()
        run_steps(self.dash, self.feed, "H1", 200)     # tanpa kill switch, 160 langkah pada data ini menghasilkan transaksi
        s = self.dash.stats("H1")
        self.assertEqual((s["trades"], s["position"]), (0, None))
        self.assertEqual(s["robot_status"], "KILL_SWITCH")


class SeparationAndStatsTests(DashBase):
    def test_m1_and_h1_stats_are_independent_and_consistent_with_ledger(self):
        self.arm("H1")
        self.arm("M15")
        run_steps(self.dash, self.feed, "H1", 160)
        h1_before = json.dumps(self.dash.stats("H1"), sort_keys=True, default=str)
        run_steps(self.dash, self.feed, "M15", 60)
        self.assertEqual(json.dumps(self.dash.stats("H1"), sort_keys=True, default=str), h1_before,
                         "menjalankan M1 mengubah statistik H1")
        s = self.dash.stats("H1")
        with open(os.path.join(self.dash.sleeve_cfg("H1", "BTCUSDT").state_dir, "paper_state.json")) as fh:
            led = json.load(fh)["ledger"]
        self.assertEqual(s["trades"], len(led["trades"]))
        self.assertGreater(s["trades"], 0, "data uji harus menghasilkan transaksi agar statistik bermakna")
        self.assertAlmostEqual(s["profit"] + s["loss"], s["net_pnl"], places=9)
        self.assertAlmostEqual(s["net_pnl"], sum(t["net_pnl"] for t in led["trades"]), places=9)
        self.assertGreaterEqual(s["drawdown_pct"], 0.0)
        self.assertTrue(0.0 <= s["win_rate"] <= 1.0)
        self.assertTrue(s["rejections"], "alasan penolakan order harus tercatat dan tampil")
        self.assertNotEqual(self.dash.stats("M15")["balance"], None)

    def test_rejection_reasons_are_aggregated_from_audit(self):
        self.arm("H1")
        run_steps(self.dash, self.feed, "H1", 120)
        rej = self.dash.stats("H1")["rejections"]
        self.assertIn("risk:short_tidak_diizinkan", rej)

    def test_changing_coin_with_open_position_is_refused_and_otherwise_requires_new_acc(self):
        self.arm("H1")
        for _ in range(500):
            run_steps(self.dash, self.feed, "H1", 1)
            if self.dash.stats("H1")["position"]:
                break
        self.assertIsNotNone(self.dash.stats("H1")["position"], "tes membutuhkan posisi terbuka")
        with self.assertRaises(D.DashboardError) as cm:
            self.dash.select("MANUAL", "H1", "ETHUSDT")
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.dash.coin["H1"], "BTCUSDT")

    def test_changing_coin_without_position_disarms(self):
        self.arm("M15")
        self.dash.select("MANUAL", "M15", "ETHUSDT")
        self.assertFalse(self.dash.armed["M15"])

    def test_selection_persists_but_armed_state_does_not(self):
        self.arm("H1", "ETHUSDT")
        d2, _ = make_dash(self.tmp)
        self.assertEqual(d2.coin["H1"], "ETHUSDT")
        self.assertFalse(d2.armed["H1"])

    def test_halt_disarms_after_step(self):
        self.arm("H1")
        run_steps(self.dash, self.feed, "H1", 3)
        cfg = self.dash.sleeve_cfg("H1", "BTCUSDT")
        SafeguardExecutionEngine(state_path=os.path.join(cfg.state_dir, "safeguard_state.json")).halt("uji")
        run_steps(self.dash, self.feed, "H1", 1)
        self.assertFalse(self.dash.armed["H1"])
        st = self.dash.stats("H1")
        self.assertTrue(st["halt"]["halted"])
        self.assertEqual(st["robot_status"], "HALT")


class AiScanTests(DashBase):
    def test_ai_scan_picks_a_coin_and_reports_ranking(self):
        s = self.dash.select("AI_SCAN", "H1")
        self.assertIn(s["timeframes"]["H1"]["coin"], COINS)
        self.assertEqual(s["scan_mode"], "AI_SCAN")
        self.assertTrue(s["scan"]["H1"])

    def test_ai_scan_without_any_eligible_coin_is_an_error(self):
        dash, feed = make_dash(self.tmp + "/x", scan_config=ScanConfig(mode="auto", min_avg_quote_volume=1e30))
        with self.assertRaises(D.DashboardError) as cm:
            dash.select("AI_SCAN", "H1")
        self.assertEqual(cm.exception.status, 422)

    def test_ai_scan_without_data_source_is_unavailable(self):
        dash = D.Dashboard(self.tmp + "/y", ".", COINS, data_source=None, token="t")
        with self.assertRaises(D.DashboardError) as cm:
            dash.select("AI_SCAN", "M15")
        self.assertEqual(cm.exception.status, 503)

    def test_invalid_coin_list_rejected(self):
        with self.assertRaises(ValueError):
            D.Dashboard(self.tmp, ".", ["BTC USDT"], token="t")


if __name__ == "__main__":
    unittest.main()
