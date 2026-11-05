"""v0.3.0 - Validasi data & kegagalan API (server Binance tiruan), paritas backtest-PAPER,
audit secret, Telegram (kirim-saja), dan pemeriksa isi ZIP."""
import http.server
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from dataclasses import replace
from unittest import mock

from btc_robot import data as DATA
from btc_robot.audit import AuditLog, redact
from btc_robot.backtest import run_backtest
from btc_robot.config import Mode, load_config, validate
from btc_robot.data import DataError, closed_only, fetch_binance_klines, generate_synthetic, load_csv, validate_candles
from btc_robot.models import BookQuote
from btc_robot.paper import paper_step
from btc_robot.telegram_notifier import TelegramNotifier

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import package_check  # noqa: E402
import verify_live_data  # noqa: E402,F401

H, M = 3_600_000, 60_000
BASE = generate_synthetic(520, 3, H)
FAKE_TOKEN = "123456789" + ":" + "A" * 35          # dirakit saat runtime: bukan literal di berkas


def rows(cs):
    return [[c.ts, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume), c.ts + H - 1, "0", 0, "0", "0", "0"]
            for c in cs]


SCENARIOS = {
    "OK": lambda: rows(BASE), "EMPTY": lambda: [],
    "DUP": lambda: rows(BASE[:50] + [BASE[49]] + BASE[50:]), "GAP": lambda: rows(BASE[:50] + BASE[52:]),
    "REVERSED": lambda: rows(BASE[::-1]), "SHORTROW": lambda: [r[:3] for r in rows(BASE)],
    "NONNUM": lambda: [["x"] + r[1:] for r in rows(BASE)], "NULLROW": lambda: [None],
    "NOTLIST": lambda: {"code": -1121, "msg": "Invalid symbol."},
}


class _Binance(http.server.BaseHTTPRequestHandler):
    hits = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        sym = re.search(r"symbol=([A-Z0-9]+)", self.path).group(1)
        _Binance.hits[sym] = _Binance.hits.get(sym, 0) + 1
        n = _Binance.hits[sym]
        code, body, hdr = 200, None, {}
        if sym == "HTTP500":
            code, body = 500, b"{}"
        elif sym == "HTTP429":
            code, body, hdr = 429, b"{}", {"Retry-After": "1"}
        elif sym == "BADJSON":
            body = b"<<bukan json"
        elif sym == "SLOW":
            threading.Event().wait(1.5)        # bukan time.sleep: test mem-patch time.sleep secara global
            body = b"[]"
        elif sym == "FLAKY":
            body = b"{}" if n <= 2 else json.dumps(rows(BASE)).encode()
            code = 500 if n <= 2 else 200
        else:
            body = json.dumps(SCENARIOS[sym]()).encode()
        self.send_response(code)
        for k, v in hdr.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class BinanceMockCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Binance)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def fetch(self, scenario, **kw):
        _Binance.hits[scenario] = 0
        with mock.patch.object(DATA.time, "sleep"):
            return fetch_binance_klines(scenario, "1h", 500, base_url=self.base, retries=3, timeout=kw.pop("timeout", 5.0), **kw)


class FetchFaultTests(BinanceMockCase):
    def test_ok_response_parses_and_validates(self):
        c = self.fetch("OK")
        self.assertEqual([x.ts for x in c], [x.ts for x in BASE])
        self.assertTrue(validate_candles(c, H).ok)

    def test_transport_and_shape_errors_become_dataerror_after_all_retries(self):
        for sc in ("HTTP500", "HTTP429", "BADJSON", "SHORTROW", "NONNUM", "NULLROW", "NOTLIST"):
            with self.subTest(sc):
                with self.assertRaises(DataError):
                    self.fetch(sc)
                self.assertEqual(_Binance.hits[sc], 3, "harus mencoba tepat 3 kali (retry terbatas)")

    def test_timeout_becomes_dataerror(self):
        with self.assertRaises(DataError):
            self.fetch("SLOW", timeout=0.3)

    def test_flaky_server_recovers_within_retries(self):
        self.assertEqual(len(self.fetch("FLAKY")), len(BASE))
        self.assertEqual(_Binance.hits["FLAKY"], 3)

    def test_returned_but_bad_series_are_flagged_by_validation(self):
        want = {"EMPTY": "data kosong", "DUP": "duplikat", "GAP": "celah", "REVERSED": "mundur"}
        for sc, text in want.items():
            with self.subTest(sc):
                rep = validate_candles(self.fetch(sc), H)
                self.assertFalse(rep.ok)
                self.assertTrue(any(text in i for i in rep.issues), rep.issues)

    def test_unclosed_last_candle_is_dropped(self):
        c = list(BASE) + generate_synthetic(1, 9, H, start_ts=BASE[-1].ts + H)
        kept = closed_only(c, H, now_ms=BASE[-1].ts + H + 5_000)
        self.assertEqual(kept[-1].ts, BASE[-1].ts)
        self.assertEqual(len(kept), len(BASE))


class FetchToPaperIntegrationTests(BinanceMockCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.cfg = replace(load_config("config.h1.example.json"), mode=Mode.PAPER, state_dir=self._td.name)
        self.now = BASE[-1].ts + H + 15_000

    def tearDown(self):
        self._td.cleanup()

    def step(self, scenario):
        return paper_step(self.cfg, now_ms=self.now, fetcher=lambda: self.fetch(scenario))

    def test_good_feed_runs_full_cycle(self):
        r = self.step("OK")
        self.assertEqual(r["status"], "ok")

    def test_bad_feeds_never_reach_the_engine_and_never_write_trading_state(self):
        expect = {"EMPTY": "data_invalid", "DUP": "data_invalid", "GAP": "data_invalid", "REVERSED": "data_invalid",
                  "HTTP500": "no_data", "BADJSON": "no_data", "NOTLIST": "no_data", "SHORTROW": "no_data"}
        for sc, status in expect.items():
            with self.subTest(sc):
                d = tempfile.mkdtemp()
                cfg = replace(self.cfg, state_dir=d)
                r = paper_step(cfg, now_ms=self.now, fetcher=lambda s=sc: self.fetch(s))
                self.assertEqual(r["status"], status)
                self.assertFalse(os.path.exists(os.path.join(d, "paper_state.json")), "state trading tidak boleh dibuat")

    def test_open_candle_is_not_processed(self):
        c = generate_synthetic(521, 3, H)
        r = paper_step(self.cfg, now_ms=c[519].ts + H + 15_000, fetcher=lambda: c)
        self.assertEqual(r["last_ts"], c[519].ts)


class VerifyLiveDataScriptTests(BinanceMockCase):
    """verify_live_data.py diuji terhadap server tiruan (bukan Binance sungguhan)."""
    def run_script(self, symbol):
        import verify_live_data
        _Binance.hits.clear()
        with mock.patch.object(DATA.time, "sleep"):
            return verify_live_data.run(symbol, self.base, now_ms=BASE[-1].ts + H + 5_000)

    def test_report_flags_bad_feed_and_passes_good_one(self):
        # 1h dan 1m memakai seri 1h yang sama di server tiruan: validasi 1m akan gagal (interval tidak cocok) -> terdeteksi
        rep = self.run_script("OK")
        names = {c["name"]: c["ok"] for c in rep["checks"]}
        self.assertTrue(names["1h:kualitas_candle"])
        self.assertFalse(names["1m:kualitas_candle"], "seri 1h dinilai sebagai 1m harus gagal")
        self.assertFalse(rep["ok"])
        bad = self.run_script("HTTP500")
        self.assertFalse(bad["ok"])
        self.assertTrue(all(not c["ok"] for c in bad["checks"] if c["name"].endswith("ambil_klines")))


class CsvTests(unittest.TestCase):
    def write(self, text):
        fd, p = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.remove, p)
        return p

    def test_bad_inputs(self):
        with self.assertRaises(DataError):
            load_csv(self.write("ts,open,high,low,close,volume\n1,2,x,4,5,6\n"))
        with self.assertRaises(DataError):
            load_csv(self.write("ts,open,high\n1,2,3\n"))
        self.assertEqual(load_csv(self.write("ts,open,high,low,close,volume\n")), [])

    def test_duplicates_disorder_and_empty_are_flagged(self):
        p = self.write("ts,open,high,low,close,volume\n3600000,1,2,1,1,1\n3600000,1,2,1,1,1\n0,1,2,1,1,1\n")
        issues = " ".join(validate_candles(load_csv(p), H).issues)
        self.assertIn("duplikat", issues)
        self.assertIn("mundur", issues)
        self.assertFalse(validate_candles([], H).ok)


class BacktestPaperParityTests(unittest.TestCase):
    """Backtest dan PAPER harus memberi transaksi yang sama pada data & asumsi yang sama."""

    def parity(self, cfg_path, itv, data, first_gap=5):
        cfg = validate(load_config(cfg_path))
        bt = run_backtest(data, cfg)
        non_final = [t for t in bt.trades if t["exit_reason"] != "end_of_data"]
        self.assertGreater(len(non_final), 5, "data uji harus menghasilkan transaksi")
        first_entry = non_final[0]["entry_ts"]
        k0 = (first_entry - data[0].ts) // itv - first_gap
        self.assertGreaterEqual(k0, 20, "harus ada riwayat untuk warm-up indikator")
        with tempfile.TemporaryDirectory() as d:
            pcfg = replace(cfg, mode=Mode.PAPER, state_dir=d, shared_kill_switch_file="")
            tight = lambda bar: BookQuote(100.0, 100.003, 1.0, 1.0)
            end = (non_final[min(len(non_final) - 1, 12)]["exit_ts"] - data[0].ts) // itv + 3
            for k in range(k0, end):
                paper_step(pcfg, now_ms=data[k].ts + itv + 15_000, fetcher=lambda k=k: data[max(0, k - 499):k + 1],
                           spread_provider=tight)
            with open(os.path.join(d, "paper_state.json")) as fh:
                ptr = json.load(fh)["ledger"]["trades"]
        cut = data[end - 1].ts
        bt_cmp = [(t["entry_ts"], t["exit_ts"], t["exit_reason"], round(t["entry_price"], 6), round(t["exit_price"], 6),
                   round(t["qty"], 8)) for t in non_final if t["exit_ts"] <= cut and t["entry_ts"] >= data[k0 + 1].ts]
        pp_cmp = [(t["entry_ts"], t["exit_ts"], t["exit_reason"], round(t["entry_price"], 6), round(t["exit_price"], 6),
                   round(t["qty"], 8)) for t in ptr]
        self.assertGreater(len(pp_cmp), 3)
        self.assertEqual(pp_cmp, bt_cmp)

    def test_h1_paper_matches_backtest(self):
        self.parity("config.h1.example.json", H, generate_synthetic(1200, 3, H))

    def test_m1_paper_matches_backtest(self):
        self.parity("config.m1.example.json", M, generate_synthetic(6000, 3, M, vol=0.0007))


class TelegramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class H_(http.server.BaseHTTPRequestHandler):
            seen = []

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                H_.seen.append((self.path, body))
                if "bot" + FAKE_TOKEN not in self.path:
                    code, out = 401, b'{"ok":false}'
                elif b"MODE_TIMEOUT" in body:
                    threading.Event().wait(1.5)
                    code, out = 200, b'{"ok":true}'
                elif b"MODE_BADJSON" in body:
                    code, out = 200, b"<<"
                elif b"MODE_NOTDICT" in body:
                    code, out = 200, b"[1]"
                elif b"MODE_NOTOK" in body:
                    code, out = 200, b'{"ok":false}'
                else:
                    code, out = 200, b'{"ok":true}'
                self.send_response(code)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                try:
                    self.wfile.write(out)
                except OSError:          # klien sudah timeout dan menutup koneksi (skenario uji)
                    pass
        cls.H = H_
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H_)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def notifier(self, token=FAKE_TOKEN, chat="42", **kw):
        return TelegramNotifier(token=token, chat_id=chat, enabled=True, api_base=kw.pop("api_base", self.base), **kw)

    def send(self, n, msg):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = n.send_message(msg)
        return ok, buf.getvalue()

    def test_success_sends_plain_message_only(self):
        self.H.seen.clear()
        ok, out = self.send(self.notifier(), "halo")
        self.assertTrue(ok)
        self.assertEqual(len(self.H.seen), 1)
        self.assertTrue(self.H.seen[0][0].endswith("/sendMessage"))

    def test_failures_return_false_and_never_print_the_token(self):
        cases = {"token_salah": (self.notifier(token="987654321" + ":" + "B" * 35), "x"),
                 "bukan_ok": (self.notifier(), "MODE_NOTOK"), "json_rusak": (self.notifier(), "MODE_BADJSON"),
                 "bukan_dict": (self.notifier(), "MODE_NOTDICT"),
                 "timeout": (self.notifier(timeout=0.3), "MODE_TIMEOUT"),
                 "koneksi_ditolak": (self.notifier(api_base="http://127.0.0.1:1"), "x")}
        for name, (n, msg) in cases.items():
            with self.subTest(name):
                ok, out = self.send(n, msg)
                self.assertFalse(ok)
                self.assertNotIn(n.token, out)
                self.assertNotIn(FAKE_TOKEN, out)

    def test_disabled_missing_or_placeholder_credentials_make_no_network_call(self):
        self.H.seen.clear()
        for n in (TelegramNotifier(token=FAKE_TOKEN, chat_id="42", enabled=False, api_base=self.base),
                  self.notifier(token=""), self.notifier(chat=""), self.notifier(token="PUT_NEW_TOKEN_HERE"),
                  self.notifier(chat="PUT_YOUR_CHAT_ID_HERE")):
            self.assertFalse(self.send(n, "x")[0])
        self.assertEqual(self.H.seen, [])

    def test_notifier_is_send_only_no_receive_or_command_capability(self):
        import btc_robot.telegram_notifier as tn
        with open(tn.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for word in ("getUpdates", "setWebhook", "getMe", "/getFile", "callback_query"):
            self.assertNotIn(word, src)
        self.assertEqual(sorted(m for m in dir(TelegramNotifier) if m.startswith("send_")),
                         ["send_halt", "send_message", "send_signal", "send_status", "send_trade_close", "send_trade_open"])

    def test_engine_survives_a_notifier_that_raises(self):
        from tests._v020_helpers import HOUR, base_cfg, flat_bars, make_engine, run_all

        class Boom:
            def __getattr__(self, name):
                def f(*a, **k):
                    raise RuntimeError("telegram jatuh " + FAKE_TOKEN)
                return f
        bars = flat_bars(10, HOUR)
        eng = make_engine(base_cfg("1h"), {2: (1.0, 50.0)}, bars)
        eng.notifier = Boom()
        run_all(eng, bars)
        self.assertEqual(eng.counters["entries"], 1)
        ev = [e for e in eng.audit.entries if e["event"] == "notification_error"]
        self.assertTrue(ev)
        self.assertNotIn(FAKE_TOKEN, json.dumps(eng.audit.entries))


class SecretHandlingTests(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_redaction_by_key_and_by_text_pattern(self):
        r = redact({"api_key": "abc", "nested": {"Telegram_Token": "x"}, "error": f"gagal https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",
                    "ok": "tidak rahasia", "list": [f"{FAKE_TOKEN}"]})
        blob = json.dumps(r)
        self.assertNotIn(FAKE_TOKEN, blob)
        self.assertEqual(r["api_key"], "[REDACTED]")
        self.assertEqual(r["ok"], "tidak rahasia")

    def test_audit_file_never_contains_a_token_even_inside_error_text(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.jsonl")
            a = AuditLog(p, "h")
            a.log("x", 1, error=f"URLError bot{FAKE_TOKEN}/getMe", details={"token": FAKE_TOKEN})
            with open(p, encoding="utf-8") as fh:
                self.assertNotIn(FAKE_TOKEN, fh.read())

    def test_repository_contains_no_secrets_and_ignores_env(self):
        bad = []
        for dirpath, dirs, files in os.walk(self.ROOT):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git", "state")]
            for f in files:
                p = os.path.join(dirpath, f)
                rel = os.path.relpath(p, self.ROOT)
                if f == ".env":
                    # Tolak .env hanya jika tracked Git.
                    import subprocess
                    tracked = subprocess.run(
                        ["git", "ls-files", "--error-unmatch", "--", rel],
                        cwd=self.ROOT,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    ).returncode == 0
                    if tracked:
                        bad.append(rel + ": .env tracked Git")
                if f.lower().endswith(package_check.TEXT_EXT) or f in (".gitignore", ".env.example"):
                    with open(p, encoding="utf-8", errors="ignore") as fh:
                        bad += package_check.check_text(rel, fh.read())
        self.assertEqual(bad, [])
        with open(os.path.join(self.ROOT, ".gitignore")) as fh:
            self.assertIn(".env", fh.read().split())
        with open(os.path.join(self.ROOT, ".env.example")) as fh:
            vals = [ln.split("=", 1)[1].strip() for ln in fh if "=" in ln and not ln.startswith("#")]
        self.assertTrue(all(v.startswith(("PUT_", "YOUR_")) or v in ("true", "false") for v in vals), vals)


class PackageCheckTests(unittest.TestCase):
    def make_zip(self, files):
        fd, p = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        self.addCleanup(os.remove, p)
        with zipfile.ZipFile(p, "w") as z:
            for n, c in files.items():
                z.writestr(n, c)
        return p

    def test_clean_zip_passes(self):
        z = self.make_zip({"olypahantstrade/README.md": "halo", "olypahantstrade/state/.gitkeep": "",
                           "olypahantstrade/.env.example": "TELEGRAM_BOT_TOKEN=PUT_NEW_TOKEN_HERE\n"})
        self.assertEqual(package_check.check_zip(z), [])

    def test_forbidden_items_are_all_detected(self):
        z = self.make_zip({"o/.env": "x", "o/state/paper_state.json": "{}", "o/btc_robot/__pycache__/a.pyc": "x",
                           "o/notes.txt": f"token {FAKE_TOKEN}", "o/k.pem": "x",
                           "o/id.md": "-----BEGIN " + "PRIVATE KEY-----\nabc"})
        out = " | ".join(package_check.check_zip(z))
        for needle in (".env", "state/", "cache Python", "token bot Telegram", "berkas kunci", "kunci privat"):
            self.assertIn(needle, out)


if __name__ == "__main__":
    unittest.main()
