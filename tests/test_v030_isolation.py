"""v0.3.0 - Isolasi M1 vs H1: state, ledger, saldo, HALT, cooldown, recovery, restart, konflik konfigurasi."""
import hashlib
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace

from btc_robot.config import Mode, load_config
from btc_robot.data import generate_synthetic
from btc_robot.identity import adopt, check_identity
from btc_robot.models import BookQuote
from btc_robot.paper import paper_step
from btc_robot.recovery import diagnose, recover
from btc_robot.safeguard import SafeguardExecutionEngine

H, M = 3_600_000, 60_000
H1_CFG, M1_CFG = "config.h1.example.json", "config.m1.example.json"
DATA_H = generate_synthetic(1200, 3, H)
DATA_M = generate_synthetic(4000, 3, M, vol=0.0007)


def read_json(p):
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def digest_dir(d):
    """Hash seluruh isi direktori (nama + isi) untuk membuktikan tidak berubah."""
    h = hashlib.sha256()
    if not os.path.isdir(d):
        return None
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            h.update(name.encode())
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


class Iso(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.h1 = self.make(H1_CFG, "h1")
        self.m1 = self.make(M1_CFG, "m1")

    def tearDown(self):
        self._td.cleanup()

    def make(self, path, name, **over):
        cfg = replace(load_config(path), mode=Mode.PAPER, state_dir=os.path.join(self.root, name),
                      shared_kill_switch_file=os.path.join(self.root, "KILL_ALL"), **over)
        return cfg

    @staticmethod
    def step(cfg, k):
        data, itv = (DATA_H, H) if cfg.interval == "1h" else (DATA_M, M)
        quote = lambda bar: BookQuote(100.0, 100.003, 1.0, 1.0)   # spread ketat untuk M1 (require_spread_check)
        return paper_step(cfg, now_ms=data[k].ts + itv + 15_000,
                          fetcher=lambda: data[max(0, k - 499):k + 1], spread_provider=quote)

    def run_both_alternating(self, n=120, h0=300, m0=700):
        for i in range(n):
            self.step(self.h1, h0 + i)
            self.step(self.m1, m0 + i * 3)


class StateSeparationTests(Iso):
    def test_each_mode_has_own_files_identity_and_ledger(self):
        self.run_both_alternating()
        for cfg, mode in ((self.h1, "h1_trend"), (self.m1, "m1_scalp")):
            self.assertEqual(read_json(os.path.join(cfg.state_dir, "identity.json"))["strategy_mode"], mode)
            for f in ("paper_state.json", "safeguard_state.json", "paper_audit.jsonl"):
                self.assertTrue(os.path.exists(os.path.join(cfg.state_dir, f)), f"{mode}: {f}")
        s1, s2 = (read_json(os.path.join(c.state_dir, "paper_state.json")) for c in (self.h1, self.m1))
        self.assertNotEqual(s1["last_ts"], s2["last_ts"])
        self.assertNotEqual(s1["ledger"], s2["ledger"])

    def test_running_one_mode_never_changes_the_other_modes_files(self):
        for i in range(60):
            self.step(self.h1, 300 + i)
        h1_before = digest_dir(self.h1.state_dir)
        for i in range(60):
            self.step(self.m1, 700 + i * 3)
        self.assertEqual(digest_dir(self.h1.state_dir), h1_before, "M1 mengubah state H1")
        m1_before = digest_dir(self.m1.state_dir)
        for i in range(60, 90):
            self.step(self.h1, 300 + i)
        self.assertEqual(digest_dir(self.m1.state_dir), m1_before, "H1 mengubah state M1")

    def test_demo_balances_are_independent(self):
        h1 = self.make(H1_CFG, "h1b", initial_equity=1000.0)
        m1 = self.make(M1_CFG, "m1b", initial_equity=500.0)
        self.step(h1, 300)
        self.step(m1, 700)
        self.assertEqual(self.step(h1, 301)["equity"], 1000.0)
        self.assertEqual(self.step(m1, 701)["equity"], 500.0)

    def test_alternating_equals_separate_runs(self):
        self.run_both_alternating()
        ref_h = self.make(H1_CFG, "ref_h1")
        ref_m = self.make(M1_CFG, "ref_m1")
        for i in range(120):
            self.step(ref_h, 300 + i)
        for i in range(120):
            self.step(ref_m, 700 + i * 3)
        for a, b in ((self.h1, ref_h), (self.m1, ref_m)):
            self.assertEqual(read_json(os.path.join(a.state_dir, "paper_state.json")),
                             read_json(os.path.join(b.state_dir, "paper_state.json")))

    def test_concurrent_threads_equal_separate_runs(self):
        errors = []

        def run(cfg, base, stride):
            try:
                for i in range(120):
                    self.step(cfg, base + i * stride)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        ts = [threading.Thread(target=run, args=(self.h1, 300, 1)), threading.Thread(target=run, args=(self.m1, 700, 3))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        ref_h, ref_m = self.make(H1_CFG, "ref_h1"), self.make(M1_CFG, "ref_m1")
        for i in range(120):
            self.step(ref_h, 300 + i)
            self.step(ref_m, 700 + i * 3)
        for a, b in ((self.h1, ref_h), (self.m1, ref_m)):
            self.assertEqual(read_json(os.path.join(a.state_dir, "paper_state.json")),
                             read_json(os.path.join(b.state_dir, "paper_state.json")))


class HaltCooldownRecoveryIsolationTests(Iso):
    def _sg(self, cfg):
        return SafeguardExecutionEngine(state_path=os.path.join(cfg.state_dir, "safeguard_state.json"))

    def test_halt_in_m1_does_not_halt_h1_and_recovery_leaves_h1_untouched(self):
        self.run_both_alternating(n=10)
        self._sg(self.m1).halt("uji_isolasi")
        self.assertTrue(self.step(self.m1, 740)["halted"])
        r = self.step(self.h1, 320)
        self.assertFalse(r["halted"])
        self.assertTrue(diagnose(self.m1)["halted"])
        self.assertFalse(diagnose(self.h1)["halted"])
        h1_before = digest_dir(self.h1.state_dir)
        recover(self.m1, "operator", "uji", True)
        self.assertEqual(digest_dir(self.h1.state_dir), h1_before, "recovery M1 menyentuh H1")
        self.assertFalse(diagnose(self.m1)["halted"])

    def test_loss_streak_and_cooldown_do_not_leak(self):
        self.run_both_alternating(n=5)
        sg = self._sg(self.m1)
        for i in range(3):
            sg.record_closed_trade(-1.0, 10 + i)
        sg.check_circuit_breaker(now=1_000.0)            # memicu cooldown M1
        self.assertGreater(self._sg(self.m1).cooldown_until, 0)
        h1_sg = self._sg(self.h1)
        self.assertEqual((h1_sg.loss_streak, h1_sg.cooldown_until, h1_sg.halted), (0, 0.0, False))

    def test_local_kill_switch_stops_only_its_mode_and_shared_kill_stops_both(self):
        self.run_both_alternating(n=5)
        open(os.path.join(self.m1.state_dir, "KILL_SWITCH"), "w").close()
        self.assertTrue(diagnose(self.m1)["kill_switch_active"])
        self.assertFalse(diagnose(self.h1)["kill_switch_active"])
        os.remove(os.path.join(self.m1.state_dir, "KILL_SWITCH"))
        open(self.h1.shared_kill_switch_file, "w").close()
        self.assertTrue(diagnose(self.m1)["kill_switch_active"])
        self.assertTrue(diagnose(self.h1)["kill_switch_active"])

    def test_shared_kill_switch_blocks_new_entries_in_both_modes(self):
        open(self.h1.shared_kill_switch_file, "w").close()
        entries = 0
        for i in range(250):
            for cfg, k in ((self.h1, 300 + i), (self.m1, 700 + i * 3)):
                entries += self.step(cfg, k)["counters"].get("entries", 0)
        self.assertEqual(entries, 0)


class RestartTests(Iso):
    def test_restart_replaying_same_window_does_not_duplicate_trades(self):
        for i in range(200):
            self.step(self.h1, 300 + i)
        before = read_json(os.path.join(self.h1.state_dir, "paper_state.json"))
        for _ in range(3):   # "restart" berulang pada candle yang sama
            r = self.step(self.h1, 499)
            self.assertEqual(r["processed_bars"], 0)
        after = read_json(os.path.join(self.h1.state_dir, "paper_state.json"))
        self.assertEqual(after["ledger"], before["ledger"])
        self.assertEqual(len(after["ledger"]["trades"]), len(before["ledger"]["trades"]))

    def test_restart_keeps_open_simulated_position(self):
        k = 300
        while True:
            r = self.step(self.h1, k)
            if r["position"]:
                break
            k += 1
            self.assertLess(k, 1100)
        pos = read_json(os.path.join(self.h1.state_dir, "paper_state.json"))["ledger"]["position"]
        self.step(self.h1, k)                      # proses baru membaca ulang dari disk
        pos2 = read_json(os.path.join(self.h1.state_dir, "paper_state.json"))["ledger"]["position"]
        self.assertEqual(pos, pos2)


class IdentityConflictTests(Iso):
    def test_same_state_dir_for_two_modes_is_refused_without_writing(self):
        shared = os.path.join(self.root, "shared")
        h1 = replace(self.h1, state_dir=shared)
        m1 = replace(self.m1, state_dir=shared)
        for i in range(20):
            self.step(h1, 300 + i)
        before = digest_dir(shared)
        r = self.step(m1, 1500)
        self.assertEqual(r["status"], "state_identity_mismatch")
        self.assertEqual(digest_dir(shared), before, "penolakan tidak boleh menulis ke direktori")
        # arah sebaliknya juga
        shared2 = os.path.join(self.root, "shared2")
        m1b, h1b = replace(self.m1, state_dir=shared2), replace(self.h1, state_dir=shared2)
        self.step(m1b, 1500)
        self.assertEqual(self.step(h1b, 300)["status"], "state_identity_mismatch")

    def test_different_symbol_or_interval_in_same_dir_is_refused(self):
        d = os.path.join(self.root, "x")
        cfg = replace(self.h1, state_dir=d)
        self.step(cfg, 300)
        self.assertEqual(self.step(replace(cfg, symbol="ETHUSDT"), 301)["status"], "state_identity_mismatch")

    def test_legacy_state_without_identity_is_refused_until_adopted(self):
        d = os.path.join(self.root, "legacy")
        os.makedirs(d)
        with open(os.path.join(d, "paper_state.json"), "w") as fh:
            json.dump({"ledger": {}, "last_ts": 1}, fh)
        cfg = replace(self.h1, state_dir=d)
        self.assertEqual(check_identity(cfg)[1], "state_identity_missing")
        self.assertEqual(self.step(cfg, 300)["status"], "state_identity_missing")
        with self.assertRaises(ValueError):
            adopt(cfg, confirmed=False)
        adopt(cfg, confirmed=True)
        self.assertTrue(check_identity(cfg)[0])

    def test_corrupt_identity_file_is_refused(self):
        self.step(self.h1, 300)
        with open(os.path.join(self.h1.state_dir, "identity.json"), "w") as fh:
            fh.write("{rusak")
        before = digest_dir(self.h1.state_dir)
        self.assertEqual(self.step(self.h1, 301)["status"], "state_identity_corrupt")
        self.assertEqual(digest_dir(self.h1.state_dir), before)

    def test_recovery_refuses_on_identity_mismatch(self):
        self.step(self.h1, 300)
        wrong = replace(self.m1, state_dir=self.h1.state_dir)
        d = diagnose(wrong)
        self.assertFalse(d["identity_ok"])
        self.assertFalse(d["recovery_allowed"])
        with self.assertRaises(ValueError):
            recover(wrong, "operator", "x", True)


class ConfigSeparationTests(unittest.TestCase):
    def test_examples_use_distinct_state_dirs_and_modes(self):
        cfgs = {n: load_config(p) for n, p in (("h1", H1_CFG), ("m1", M1_CFG), ("mtf", "config.mtf.example.json"))}
        self.assertEqual(len({c.state_dir for c in cfgs.values()}), 3)
        self.assertEqual(len({c.sleeve_id for c in cfgs.values()}), 3)
        self.assertEqual({c.interval for c in cfgs.values()}, {"1h", "1m", "15m"})
        self.assertNotEqual(cfgs["h1"].strategy, cfgs["m1"].strategy)
        self.assertNotEqual(cfgs["h1"].risk, cfgs["m1"].risk)


if __name__ == "__main__":
    unittest.main()
