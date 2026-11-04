"""v0.2.0 - Tes KEGAGALAN: koneksi putus, data rusak, restart dengan posisi terbuka, crash saat simpan,
status order tidak diketahui, HALT, dan pemulihan (tanpa menghapus riwayat / melewati batas risiko)."""
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from btc_robot import paper
from btc_robot.audit import verify_file
from btc_robot.config import Mode, config_from_dict
from btc_robot.data import DataError, generate_synthetic
from btc_robot.execution import ExecutionRejected, SimulatedExecutionAdapter
from btc_robot.models import Candle
from btc_robot.paper import paper_step
from btc_robot.recovery import diagnose, recover
from btc_robot.safeguard import SafeguardExecutionEngine

from tests._v020_helpers import HOUR, base_cfg, flat_bars, make_engine

M15 = 900_000
START = 1_700_000_100_000 // M15 * M15


def read_bytes(p):
    with open(p, "rb") as fh:
        return fh.read()


def read_json(p):
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_json_or_none(p):
    return read_json(p) if os.path.exists(p) else None


def read_lines(p):
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def paper_cfg(tmp, **risk):
    r = {"max_data_age_seconds": 900}
    r.update(risk)
    return replace(config_from_dict({"mode": "PAPER", "interval": "15m", "strategy_mode": "mtf_1h_30m_15m",
                                     "risk": r}), state_dir=tmp)


class PaperCase(unittest.TestCase):
    def setUp(self):
        self.c = generate_synthetic(2400, 11, M15, start_ts=START)
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.cfg = paper_cfg(self.tmp)

    def tearDown(self):
        self._td.cleanup()

    def step(self, k, cfg=None, candles=None):
        data = candles if candles is not None else self.c[max(0, k - 499):k + 1]
        return paper_step(cfg or self.cfg, now_ms=self.c[k].ts + M15 + 15_000, fetcher=lambda: data)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def digest(self, name="paper_state.json"):
        p = self.path(name)
        return hashlib.sha256(read_bytes(p)).hexdigest() if os.path.exists(p) else None

    def state(self):
        return read_json(self.path("paper_state.json"))

    def run_until(self, pred, start=700, limit=2300):
        k = start
        r = self.step(k)
        while not pred(r):
            k += 1
            self.assertLess(k, limit, "kondisi tidak tercapai pada data uji")
            r = self.step(k)
        return k, r


class ConnectionAndDataFailureTests(PaperCase):
    def test_connection_lost_leaves_state_untouched_and_resumes(self):
        for k in range(700, 706):
            self.step(k)
        before = (self.digest(), self.digest("safeguard_state.json"))
        for exc in (DataError("koneksi terputus"), ConnectionError("reset"), TimeoutError("lambat")):
            def boom(e=exc):
                raise e
            r = paper_step(self.cfg, now_ms=self.c[706].ts + M15 + 15_000, fetcher=boom)
            self.assertEqual(r["status"], "no_data")
            self.assertEqual((self.digest(), self.digest("safeguard_state.json")), before)
        r = self.step(706)
        self.assertEqual((r["status"], r["processed_bars"]), ("ok", 1))  # lanjut normal tanpa kehilangan candle

    def test_invalid_data_is_refused_without_touching_state(self):
        for k in range(700, 704):
            self.step(k)
        before = self.digest()
        window = list(self.c[204:704])
        bad_variants = {
            "nan": window[:-1] + [Candle(window[-1].ts, float("nan"), 1, 1, 1, 1)],
            "duplikat_ts": window[:-1] + [window[-1], window[-1]],
            "urutan_terbalik": window[::-1],
            "ohlc_tidak_masuk_akal": window[:-1] + [Candle(window[-1].ts, 100, 90, 110, 100, 1)],
        }
        for name, data in bad_variants.items():
            with self.subTest(name):
                r = paper_step(self.cfg, now_ms=self.c[703].ts + M15 + 15_000, fetcher=lambda d=data: d)
                self.assertEqual(r["status"], "data_invalid")
                self.assertEqual(self.digest(), before)

    def test_stale_data_is_refused(self):
        for k in range(700, 703):
            self.step(k)
        before = self.digest()
        r = paper_step(self.cfg, now_ms=self.c[702].ts + M15 + 3 * 3600_000, fetcher=lambda: self.c[203:703])
        self.assertEqual(r["status"], "data_invalid")
        self.assertEqual(self.digest(), before)

    def test_kill_switch_blocks_entries_but_not_exits(self):
        k, _ = self.run_until(lambda r: r["position"] is not None)
        open(self.path("KILL_SWITCH"), "w").close()
        trades0 = len(self.state()["ledger"]["trades"])
        entries_seen = 0
        for j in range(k + 1, k + 400):
            r = self.step(j)
            entries_seen += r["counters"].get("entries", 0)
        self.assertEqual(entries_seen, 0, "KILL_SWITCH: tidak boleh ada entry baru")
        self.assertGreater(len(self.state()["ledger"]["trades"]), trades0, "posisi lama tetap bisa keluar (stop/TP)")


class RestartAndCrashTests(PaperCase):
    def test_restart_with_open_position_restores_it_and_enforces_stop(self):
        k, r = self.run_until(lambda r: r["position"] is not None)
        st = self.state()
        pos = st["ledger"]["position"]
        self.assertIsNotNone(pos)                      # posisi benar-benar tersimpan di disk
        entries_before = len(st["ledger"]["trades"])
        stop = pos["stop_price"]
        last = self.c[k]
        crash = Candle(last.ts + M15, last.close, last.close, stop * 0.98, stop * 0.985, 100.0)
        window = list(self.c[max(0, k - 498):k + 1]) + [crash]
        r = paper_step(self.cfg, now_ms=crash.ts + M15 + 15_000, fetcher=lambda: window)  # "proses baru" membaca dari disk
        self.assertEqual(r["status"], "ok")
        st2 = self.state()
        self.assertIsNone(st2["ledger"]["position"])
        self.assertEqual(len(st2["ledger"]["trades"]), entries_before + 1)
        self.assertEqual(st2["ledger"]["trades"][-1]["exit_reason"], "stop")

    def test_crash_while_saving_state_keeps_old_state_and_replay_is_consistent(self):
        """Crash tepat saat commit state: berkas lama utuh; ulang langkah itu menghasilkan state yang SAMA
        dengan run tanpa crash pada SETIAP langkah (termasuk loss_streak safeguard, tidak terhitung dua kali)."""
        ref_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(ref_dir, ignore_errors=True))
        ref_cfg = paper_cfg(ref_dir)
        # langkah crash dipilih sampai mencakup penutupan trade rugi (data seed 11: 789, 1277)
        crash_steps = {704, 789, 833, 900, 1277}
        real = os.replace

        def bad(src, dst, *a, **kw):
            if str(dst).endswith("paper_state.json"):
                raise OSError("disk penuh")
            return real(src, dst, *a, **kw)

        compared = 0
        for k in range(700, 1300):
            self.step(k, ref_cfg)
            if k in crash_steps:
                before = self.digest()
                with mock.patch.object(paper.os, "replace", bad):
                    with self.assertRaises(OSError):
                        self.step(k)
                self.assertEqual(self.digest(), before, "state lama harus utuh setelah crash")
            self.step(k)
            for name in ("paper_state.json", "safeguard_state.json"):
                self.assertEqual(read_json_or_none(self.path(name)), read_json_or_none(os.path.join(ref_dir, name)),
                                 f"{name} menyimpang dari run tanpa crash pada langkah {k}")
            compared += 1
        self.assertEqual(compared, 600)
        # audit bersifat at-least-once (langkah yang diulang tercatat lagi) tetapi rantai hash tetap valid
        self.assertTrue(verify_file(self.path("paper_audit.jsonl")))

    def test_corrupt_state_is_refused_not_reset(self):
        for k in range(700, 704):
            self.step(k)
        p = self.path("paper_state.json")
        with open(p, "w") as fh:
            fh.write('{"ledger": {"cash": 1')                 # terpotong
        before = self.digest()
        r = self.step(704)
        self.assertEqual(r["status"], "state_corrupt")
        self.assertEqual(self.digest(), before, "berkas rusak tidak boleh ditimpa/di-reset diam-diam")

    def test_structurally_wrong_state_is_refused(self):
        for k in range(700, 703):
            self.step(k)
        with open(self.path("paper_state.json"), "w") as fh:
            json.dump({"ledger": {"cash": "bukan-angka"}}, fh)
        self.assertEqual(self.step(703)["status"], "state_corrupt")

    def test_second_process_is_blocked_by_lock(self):
        try:
            import fcntl
        except ImportError:
            self.skipTest("fcntl tidak tersedia")
        os.makedirs(self.tmp, exist_ok=True)
        with open(self.path(".paper.lock"), "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.step(700)["status"], "locked")


class UnknownOrderStatusTests(unittest.TestCase):
    """Status order TIDAK DIKETAHUI (timeout saat submit) => HALT, jangan coba lagi."""

    def _run(self, exc):
        class Flaky(SimulatedExecutionAdapter):
            def submit_entry(self, order, bar):
                raise exc
        bars = flat_bars(12, HOUR)
        eng = make_engine(base_cfg("1h"), {2: (1.0, 50.0), 5: (1.0, 50.0)}, bars, adapter_cls=Flaky)
        for i in range(len(bars)):
            eng.process_bar(bars, i)
        return eng

    def test_timeout_during_submit_halts_and_blocks_retry(self):
        eng = self._run(TimeoutError("exchange timeout"))
        self.assertTrue(eng.risk.halted)
        self.assertTrue(eng.risk.halt_reason.startswith("exec_unknown"))
        self.assertEqual(eng.counters["exec_unknown"], 1)       # hanya SATU percobaan: tidak ada retry buta
        self.assertEqual(eng.counters["entries"], 0)
        self.assertIsNone(eng.ledger.position)
        self.assertIsNone(eng.pending)

    def test_explicit_rejection_halts_too(self):
        eng = self._run(ExecutionRejected("ditolak"))
        self.assertTrue(eng.risk.halted)
        self.assertEqual(eng.counters["exec_error"], 1)


class HaltRecoveryTests(PaperCase):
    def _force_safeguard_halt(self):
        sg = SafeguardExecutionEngine(state_path=self.path("safeguard_state.json"))
        sg.halt("invalid_closed_trade_pnl")

    def test_safeguard_only_halt_is_visible_and_recoverable(self):
        """Regresi: dulu diagnose melapor 'tidak halted' dan recover menolak -> buntu."""
        for k in range(700, 703):
            self.step(k)
        self._force_safeguard_halt()
        d = diagnose(self.cfg)
        self.assertTrue(d["halted"])
        self.assertEqual(d["halt_sources"], ["safeguard"])
        self.assertTrue(d["recovery_allowed"])
        recover(self.cfg, "operator", "pnl tidak valid sudah diperiksa", True)
        self.assertFalse(diagnose(self.cfg)["halted"])
        self.assertFalse(SafeguardExecutionEngine(state_path=self.path("safeguard_state.json")).halted)

    def test_recovery_requires_explicit_confirmation(self):
        for k in range(700, 703):
            self.step(k)
        self._force_safeguard_halt()
        with self.assertRaises(ValueError):
            recover(self.cfg, "operator", "alasan", False)
        self.assertTrue(diagnose(self.cfg)["halted"])

    def test_recovery_refused_when_not_halted(self):
        for k in range(700, 703):
            self.step(k)
        with self.assertRaises(ValueError):
            recover(self.cfg, "operator", "alasan", True)

    def test_recovery_keeps_history_ledger_position_and_loss_streak(self):
        k, _ = self.run_until(lambda r: r["position"] is not None)
        sg_path = self.path("safeguard_state.json")
        sg = SafeguardExecutionEngine(state_path=sg_path)
        sg.record_closed_trade(-1.0, 10)
        sg.record_closed_trade(-1.0, 11)           # loss_streak = 2
        sg.halt("diuji")
        audit_before = read_lines(self.path("paper_audit.jsonl"))
        ledger_before = self.state()["ledger"]
        recover(self.cfg, "operator", "diperiksa", True)
        audit_after = read_lines(self.path("paper_audit.jsonl"))
        self.assertEqual(audit_after[:len(audit_before)], audit_before, "riwayat audit tidak boleh diubah/dihapus")
        self.assertEqual(len(audit_after), len(audit_before) + 1)
        self.assertEqual(json.loads(audit_after[-1])["event"], "paper_halt_recovered")
        self.assertEqual(self.state()["ledger"], ledger_before, "ledger & posisi tidak di-reset")
        self.assertEqual(SafeguardExecutionEngine(state_path=sg_path).loss_streak, 2,
                         "loss_streak tidak boleh dihapus oleh pemulihan")

    def test_recovery_does_not_bypass_drawdown_limit(self):
        """Setelah recover, drawdown masih di atas batas => halt terpasang lagi pada sinyal berikutnya."""
        cfg = paper_cfg(self.tmp, max_drawdown_pct=0.3)
        k = 700
        r = self.step(k, cfg)
        while not r["halted"]:
            k += 1
            self.assertLess(k, 2300)
            r = self.step(k, cfg)
        self.assertTrue(diagnose(cfg)["halted"])
        recover(cfg, "operator", "uji", True)
        self.assertFalse(diagnose(cfg)["halted"])
        relatched = False
        for j in range(k + 1, k + 500):
            if self.step(j, cfg)["halted"]:
                relatched = True
                break
        self.assertTrue(relatched, "peak equity dipertahankan: batas drawdown harus berlaku lagi")

    def test_gap_halt_with_open_position_requires_recovery(self):
        k, _ = self.run_until(lambda r: r["position"] is not None)
        r = self.step(k + 800)
        self.assertTrue(r["halted"])
        self.assertTrue(diagnose(self.cfg)["halted"])
        self.assertGreater(r["gap_missed_bars"], 0)


class SafeguardIdempotencyTests(unittest.TestCase):
    def test_same_trade_not_counted_twice(self):
        sg = SafeguardExecutionEngine()
        sg.record_closed_trade(-1.0, 100)
        sg.record_closed_trade(-1.0, 100)   # replay langkah yang sama
        self.assertEqual(sg.loss_streak, 1)
        sg.record_closed_trade(-1.0, 200)
        self.assertEqual(sg.loss_streak, 2)

    def test_distinct_trade_ids_same_timestamp(self):
        sg = SafeguardExecutionEngine()
        sg.record_closed_trade(-1.0, 100, trade_id="trade-a")
        sg.record_closed_trade(-1.0, 100, trade_id="trade-b")
        sg.record_closed_trade(-1.0, 100, trade_id="trade-c")
        self.assertEqual(sg.loss_streak, 0)
        self.assertGreater(sg.cooldown_until, 0)

    def test_duplicate_trade_id_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "safeguard.json")
            sg = SafeguardExecutionEngine(state_path=path)
            sg.record_closed_trade(-1.0, 100, trade_id="trade-a")
            restored = SafeguardExecutionEngine(state_path=path)
            restored.record_closed_trade(-1.0, 100, trade_id="trade-a")
            self.assertEqual(restored.loss_streak, 1)

    def test_backward_compatible_without_timestamp(self):
        sg = SafeguardExecutionEngine()
        sg.record_closed_trade(-1.0)
        sg.record_closed_trade(-1.0)
        self.assertEqual(sg.loss_streak, 2)


if __name__ == "__main__":
    unittest.main()
