import dataclasses
import json
import os
import tempfile
import unittest
from dataclasses import replace

from btc_robot import gates
from btc_robot.audit import AuditLog, verify_chain
from btc_robot.backtest import run_backtest
from btc_robot.config import LIVE_CONFIRM_PHRASE, Config, ConfigError, config_hash
from btc_robot.data import generate_synthetic
from btc_robot.statemachine import FailureKind, IllegalTransition, State, StateMachine, resolve
from btc_robot.timecontract import (ClockHealth, DataStatus, DecisionSnapshot, Finality,
                                    make_decision_id, stamp, usable_for_decision)

NOW = 1_700_000_100_000


def st(value="x", event=NOW - 5_000, recv=NOW - 4_000, max_age=60_000, **kw):
    return stamp("px", value, "src", event, recv, NOW, max_age, **kw)


class TestTimeContract(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(st().status, DataStatus.KNOWN)
        self.assertEqual(st(value=None).status, DataStatus.MISSING)
        self.assertEqual(st(event=None).status, DataStatus.UNKNOWN)
        self.assertEqual(st(event=NOW - 500_000, recv=NOW - 499_000).status, DataStatus.STALE)
        self.assertEqual(st(conflicting=True).status, DataStatus.CONFLICTING)
        self.assertEqual(st(recv=NOW + 60_000).status, DataStatus.UNKNOWN)           # diterima di masa depan
        self.assertEqual(st(event=NOW - 1_000, recv=NOW - 20_000).status, DataStatus.UNKNOWN)  # diterima sebelum terjadi

    def test_unknown_is_never_neutral(self):
        for s in (st(value=None), st(event=None), st(conflicting=True), st(event=NOW - 500_000, recv=NOW - 499_000)):
            ok, _ = usable_for_decision(s, NOW)
            self.assertFalse(ok, s.status)

    def test_late_arriving_data_not_usable_for_earlier_decision(self):
        s = st(recv=NOW - 1_000)
        self.assertTrue(usable_for_decision(s, NOW)[0])
        ok, why = usable_for_decision(s, NOW - 2_000)  # keputusan dibuat SEBELUM data diterima
        self.assertFalse(ok)
        self.assertIn("setelah_keputusan", why)

    def test_provisional_blocked_by_default(self):
        s = st(finality=Finality.PROVISIONAL)
        self.assertFalse(usable_for_decision(s, NOW)[0])
        self.assertTrue(usable_for_decision(s, NOW, allow_provisional=True)[0])

    def test_clock_health_is_hard_dependency(self):
        c = ClockHealth(1_000)
        self.assertTrue(c.check(NOW, NOW + 500)[0])
        self.assertFalse(c.check(NOW, NOW + 5_000)[0])
        self.assertFalse(c.check(NOW, None)[0])   # referensi hilang = tidak sehat

    def test_snapshot_immutable_deterministic_and_checks_inputs(self):
        a, b = st("1"), st("2", max_age=60_000)
        v = [("strategy", "s/1"), ("config", "abc")]
        s1 = DecisionSnapshot.build(NOW, [a, b], v)
        s2 = DecisionSnapshot.build(NOW, [a, b], list(reversed(v)))
        self.assertEqual(s1.decision_id, s2.decision_id)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            s1.decision_id = "x"  # type: ignore[misc]
        self.assertEqual(s1.unusable(), [])
        bad = DecisionSnapshot.build(NOW, [a, st(value=None)], v)
        self.assertNotEqual(bad.decision_id, s1.decision_id)
        self.assertEqual(len(bad.unusable()), 1)

    def test_decision_id_depends_on_config_and_data(self):
        base = make_decision_id("s/1", 1, "fp", "cfg")
        self.assertEqual(base, make_decision_id("s/1", 1, "fp", "cfg"))
        self.assertNotEqual(base, make_decision_id("s/1", 1, "fp", "cfg2"))
        self.assertNotEqual(base, make_decision_id("s/1", 1, "fp2", "cfg"))

    def test_engine_logs_decision_id_consistently(self):
        res = run_backtest(generate_synthetic(2000, 5), Config())
        by_id = {}
        for e in res.audit.entries:
            did = e["payload"].get("decision_id")
            if did:
                by_id.setdefault(did, []).append(e["event"])
        self.assertTrue(by_id)
        entered = [ev for ev in by_id.values() if "entry" in ev]
        self.assertTrue(entered)
        for evs in entered:   # signal -> risk_decision -> entry memakai ID yang sama
            self.assertEqual(evs[:3], ["signal", "risk_decision", "entry"])


class TestStateMachine(unittest.TestCase):
    def happy(self, sm):
        for to in (State.SYNCING, State.READY, State.ANALYZING, State.SIGNAL, State.RISK_CHECK,
                   State.EXECUTING, State.POSITION, State.EXITING, State.RECONCILING, State.READY):
            sm.transition(to, "t")

    def test_happy_path_and_audit(self):
        audit = AuditLog(None, "c")
        sm = StateMachine(audit)
        self.happy(sm)
        self.assertEqual(sm.state, State.READY)
        self.assertTrue(verify_chain(audit.entries))

    def test_illegal_jumps_blocked(self):
        for start_path, bad in (([], State.POSITION), ([State.SYNCING, State.READY], State.EXECUTING),
                                ([State.SYNCING, State.READY, State.ANALYZING], State.EXECUTING),
                                ([State.SYNCING], State.POSITION)):
            sm = StateMachine()
            for s in start_path:
                sm.transition(s, "t")
            with self.assertRaises(IllegalTransition):
                sm.transition(bad, "t")

    def test_any_state_to_safe_and_exit_only_by_recover(self):
        sm = StateMachine()
        sm.transition(State.SYNCING, "t")
        sm.transition(State.SAFE, "x")
        with self.assertRaises(IllegalTransition):
            sm.transition(State.SYNCING, "bypass")
        with self.assertRaises(IllegalTransition):
            sm.recover("", "tanpa nama")
        sm.recover("pemilik", "sudah dicek")
        self.assertEqual(sm.state, State.SYNCING)

    def test_recover_refused_while_failure_active(self):
        sm = StateMachine()
        sm.transition(State.SYNCING, "t")
        sm.update_failures([FailureKind.CLOCK])
        self.assertEqual(sm.state, State.SAFE)
        with self.assertRaises(IllegalTransition):
            sm.recover("pemilik", "terlalu cepat")
        sm.update_failures([])
        sm.recover("pemilik", "jam pulih")
        self.assertEqual(sm.state, State.SYNCING)

    def test_failure_table(self):
        r = resolve([FailureKind.MARKET_DATA])
        self.assertTrue(r.block_new_entries and r.go_safe)
        r = resolve([FailureKind.RISK_ENGINE])
        self.assertTrue(r.block_new_entries and not r.go_safe)
        r = resolve([FailureKind.EXCHANGE_API])
        self.assertTrue(r.block_new_entries and r.reconcile)
        r = resolve([FailureKind.NEWS])
        self.assertTrue(r.degraded and not r.block_new_entries and not r.go_safe)
        r = resolve([FailureKind.AI])
        self.assertTrue(r.fallback_or_safe)
        r = resolve([FailureKind.DB_LOGGING])
        self.assertTrue(r.go_safe)
        for k in (FailureKind.CONFLICTING_DATA, FailureKind.STALE_OR_MISSING_DATA):
            self.assertTrue(resolve([k]).reject_decision)
        r = resolve([FailureKind.KILL_SWITCH])
        self.assertTrue(r.block_new_entries and r.emergency_handling)

    def test_precedence_and_flags_never_loosen(self):
        r = resolve([FailureKind.NEWS, FailureKind.KILL_SWITCH, FailureKind.AI])
        self.assertEqual(r.layer, "SAFETY")
        self.assertTrue(r.block_new_entries and r.degraded and r.fallback_or_safe)
        r = resolve([FailureKind.NEWS, FailureKind.EXCHANGE_API, FailureKind.CONFLICTING_DATA])
        self.assertEqual(r.layer, "EXCHANGE")
        self.assertIsNone(resolve([]))

    def test_entries_blocked_by_failures_and_state(self):
        sm = StateMachine()
        self.assertFalse(sm.new_entries_allowed)  # INIT
        sm.transition(State.SYNCING, "t")
        sm.transition(State.READY, "t")
        self.assertTrue(sm.new_entries_allowed)
        sm.update_failures([FailureKind.KILL_SWITCH])
        self.assertFalse(sm.new_entries_allowed)
        with self.assertRaises(IllegalTransition):
            sm.transition(State.ANALYZING, "t")
        sm.update_failures([FailureKind.NEWS])   # news saja: tidak memblok entri
        self.assertTrue(sm.new_entries_allowed)


class TestGatesG0toG8(unittest.TestCase):
    def write(self, td, passed_upto, approval_cfg=None):
        data = {"gates": {g: {"passed": g <= passed_upto, "evidence": f"ev-{g}" if g <= passed_upto else None}
                          for g in gates.GATE_IDS}}
        if approval_cfg is not None:
            data["owner_approval"] = {"approved_by": "pemilik", "config_hash": config_hash(approval_cfg)}
        p = os.path.join(td, "g.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return p

    def test_nine_gates(self):
        self.assertEqual(gates.GATE_IDS, [f"G{i}" for i in range(9)])

    def test_live_needs_g0_to_g6_plus_approval_plus_env(self):
        env = {"BTC_ROBOT_LIVE_CONFIRM": LIVE_CONFIRM_PHRASE}
        with tempfile.TemporaryDirectory() as td:
            cfg = Config()
            # G5 saja (belum testnet) -> terkunci
            c1 = replace(cfg, gates_file=self.write(td, "G5", replace(cfg, gates_file="x")))
            with self.assertRaises(ConfigError) as cm:
                gates.require_live_unlock(c1, env)
            self.assertIn("G6", str(cm.exception))
            # G0–G6 lulus tetapi persetujuan terikat ke config LAIN -> terkunci
            other = replace(cfg, initial_equity=5.0)
            c2 = replace(cfg, gates_file=self.write(td, "G6", other))
            with self.assertRaises(ConfigError):
                gates.require_live_unlock(c2, env)
            # tanpa env -> terkunci
            with self.assertRaises(ConfigError):
                gates.require_live_unlock(c2, {})
            # semua benar -> lolos pemeriksaan gerbang (adapter live tetap belum ada)
            path = os.path.join(td, "ok.json")
            c3 = replace(cfg, gates_file=path)
            good = self.write(td, "G6", c3)
            os.replace(good, path)
            gates.require_live_unlock(c3, env)  # tidak melempar

    def test_evidence_required(self):
        data = {"gates": {g: {"passed": True, "evidence": None} for g in gates.GATE_IDS}}
        self.assertEqual(gates.missing_gates(data, "G6"), gates.GATE_IDS[:7])


if __name__ == "__main__":
    unittest.main()
