"""v0.3.0 - Jalur order: urutan HALT -> Risk -> Safe Guard -> Portofolio -> HALT final -> eksekusi,
integrasi StateMachine, cooldown berjangkar, batas risiko, stop/TP tidak valid, duplikat/retry."""
import os
import tempfile
import unittest
from dataclasses import replace

from btc_robot.config import Mode, load_config
from btc_robot.data import generate_synthetic
from btc_robot.engine import PIPELINE
from btc_robot.execution import ExecutionRejected, SimulatedExecutionAdapter
from btc_robot.models import Candle, Side, Signal
from btc_robot.paper import paper_step
from btc_robot.recovery import recover
from btc_robot.safeguard import SafeguardExecutionEngine
from btc_robot.statemachine import FailureKind, IllegalTransition, State

from tests._v020_helpers import HOUR, StubStrategy, base_cfg, flat_bars, make_engine, run_all, with_bar
from tests.test_v020_failure_recovery import PaperCase, read_json, paper_cfg


def wrap(obj, name, label, log):
    orig = getattr(obj, name)

    def f(*a, **k):
        log.append(label)
        return orig(*a, **k)
    setattr(obj, name, f)


def instrumented(cfg, plan, bars, gate=(True, "ok")):
    log = []
    holder = {}

    def gate_fn(order, bar):
        log.append("portfolio")
        if callable(gate):
            return gate(holder["eng"])
        return gate
    eng = make_engine(cfg, plan, bars, entry_gate=gate_fn)
    holder["eng"] = eng
    wrap(eng, "_halt_reason", "halt", log)
    wrap(eng, "_final_halt_reason", "halt_final", log)
    wrap(eng.risk, "evaluate", "risk", log)
    wrap(eng.safeguard, "check_circuit_breaker", "safeguard", log)
    wrap(eng.adapter, "submit_entry", "submit", log)
    return eng, log


class PipelineOrderTests(unittest.TestCase):
    def setUp(self):
        self.bars = flat_bars(10, HOUR)
        self.plan = {2: (1.0, 50.0)}

    def test_declared_order_constant(self):
        self.assertEqual(PIPELINE, ("halt", "risk", "safeguard", "portfolio", "halt_final", "execute"))

    def test_approved_order_passes_every_stage_in_order(self):
        eng, log = instrumented(base_cfg("1h"), self.plan, self.bars)
        run_all(eng, self.bars)
        self.assertEqual(log, ["halt", "risk", "safeguard", "portfolio", "halt_final", "submit"])
        self.assertEqual(eng.counters["entries"], 1)

    def test_risk_veto_stops_pipeline_before_safeguard_and_execution(self):
        eng, log = instrumented(base_cfg("1h", min_edge_cost_mult=2.0), {2: (1.0, 0.3)}, self.bars)
        run_all(eng, self.bars)
        self.assertEqual(log, ["halt", "risk"])

    def test_safeguard_veto_stops_before_portfolio_and_execution(self):
        eng, log = instrumented(base_cfg("1h"), self.plan, self.bars)
        eng.safeguard.cooldown_until = 9e12
        run_all(eng, self.bars)
        self.assertEqual(log, ["halt", "risk", "safeguard"])
        self.assertEqual(eng.counters["safeguard_reject:cooldown"], 1)

    def test_portfolio_veto_stops_before_execution(self):
        eng, log = instrumented(base_cfg("1h"), self.plan, self.bars, gate=(False, "penuh"))
        run_all(eng, self.bars)
        self.assertEqual(log, ["halt", "risk", "safeguard", "portfolio"])

    def test_halt_latched_after_signal_stops_everything_at_stage_one(self):
        eng, log = instrumented(base_cfg("1h"), self.plan, self.bars)
        for i in range(3):
            eng.process_bar(self.bars, i)          # sinyal di bar 2 -> pending
        eng.risk.halt("halt_tengah_jalan")
        eng.process_bar(self.bars, 3)
        self.assertEqual(log, ["halt"])
        self.assertEqual(eng.counters["halt_reject:risk_halt"], 1)
        self.assertIsNone(eng.ledger.position)

    def test_halt_appearing_during_pipeline_is_caught_by_final_check(self):
        eng, log = instrumented(base_cfg("1h"), self.plan, self.bars, gate=lambda e: (e.risk.halt("race"), (True, "ok"))[1])
        run_all(eng, self.bars)
        self.assertEqual(log, ["halt", "risk", "safeguard", "portfolio", "halt_final"])   # tidak ada 'submit'
        self.assertEqual(eng.counters["halt_reject:final"], 1)
        self.assertEqual(eng.counters["entries"], 0)

    def test_kill_switch_created_during_pipeline_is_caught_by_final_check(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = replace(base_cfg("1h"), state_dir=d)
            ks = cfg.kill_switch_file
            eng, log = instrumented(cfg, self.plan, self.bars, gate=lambda e: (open(ks, "w").close(), (True, "ok"))[1])
            run_all(eng, self.bars)
            self.assertEqual(log[-1], "halt_final")
            self.assertEqual(eng.counters["entries"], 0)

    def test_order_not_signed_by_risk_engine_cannot_be_executed(self):
        seen = {}
        eng, _ = instrumented(base_cfg("1h"), self.plan, self.bars)
        orig = eng.adapter.submit_entry

        def spy(order, bar):
            seen["order"], seen["bar"] = order, bar
            return orig(order, bar)
        eng.adapter.submit_entry = spy
        run_all(eng, self.bars)
        forged = replace(seen["order"], qty=seen["order"].qty * 2, client_order_id="forged-1")
        with self.assertRaises(ExecutionRejected):
            SimulatedExecutionAdapter(eng.cfg.costs, Mode.BACKTEST, eng.risk.verify).submit_entry(forged, seen["bar"])


class DuplicateAndRetryTests(unittest.TestCase):
    def test_same_order_id_is_idempotent_at_adapter(self):
        bars = flat_bars(10, HOUR)
        eng, _ = instrumented(base_cfg("1h"), {2: (1.0, 50.0)}, bars)
        captured = []
        orig = eng.adapter.submit_entry
        eng.adapter.submit_entry = lambda o, b: (captured.append((o, b)), orig(o, b))[1]
        run_all(eng, bars)
        order, bar = captured[0]
        f1 = eng.adapter.seen[order.client_order_id]
        f2 = orig(order, bar)
        self.assertEqual(f1, f2)
        self.assertEqual(eng.adapter.duplicates_blocked, 1)

    def test_replayed_begin_bar_does_not_open_second_position(self):
        bars = flat_bars(10, HOUR)
        eng = make_engine(base_cfg("1h"), {2: (1.0, 50.0)}, bars)
        for i in range(4):
            eng.process_bar(bars, i)
        eng.begin_bar(bars[3])           # pemrosesan ulang bar yang sama
        eng.begin_bar(bars[3])
        self.assertEqual(eng.counters["entries"], 1)

    def test_rejected_submit_is_not_retried_and_halts(self):
        calls = []

        class Rejecting(SimulatedExecutionAdapter):
            def submit_entry(self, order, bar):
                calls.append(1)
                raise ExecutionRejected("ditolak")
        bars = flat_bars(14, HOUR)
        eng = make_engine(base_cfg("1h"), {2: (1.0, 50.0), 6: (1.0, 50.0)}, bars, adapter_cls=Rejecting)
        run_all(eng, bars)
        self.assertEqual(len(calls), 1, "tidak boleh ada retry otomatis; sinyal berikutnya diblok HALT")
        self.assertTrue(eng.risk.halted)


class StateMachineIntegrationTests(unittest.TestCase):
    def _trade_engine(self):
        bars = flat_bars(12, HOUR)
        with_bar(bars, 3, 100, 100.05, 98.5, 98.8)       # stop 1% kena di candle entry
        eng = make_engine(base_cfg("1h"), {2: (1.0, 50.0)}, bars)
        run_all(eng, bars)
        return eng

    def test_full_trade_walks_the_state_machine_in_order(self):
        eng = self._trade_engine()
        visited = [(a.value, b.value) for a, b, _ in eng.machine.history]
        wanted = [("READY", "ANALYZING"), ("ANALYZING", "SIGNAL"), ("SIGNAL", "RISK_CHECK"),
                  ("RISK_CHECK", "EXECUTING"), ("EXECUTING", "POSITION"), ("POSITION", "EXITING"),
                  ("EXITING", "RECONCILING"), ("RECONCILING", "READY")]
        it = iter(visited)
        for step in wanted:
            self.assertTrue(any(step == v for v in it), f"transisi {step} tidak ditemukan berurutan")
        self.assertEqual(eng.machine.state, State.READY)

    def test_vetoed_signal_returns_to_ready(self):
        bars = flat_bars(10, HOUR)
        eng = make_engine(base_cfg("1h", min_edge_cost_mult=2.0), {2: (1.0, 0.3)}, bars)
        run_all(eng, bars)
        self.assertEqual(eng.machine.state, State.READY)
        self.assertIn((State.RISK_CHECK, State.READY), [(a, b) for a, b, _ in eng.machine.history])

    def test_unknown_order_status_moves_machine_to_reconciling_and_blocks_entries(self):
        class Flaky(SimulatedExecutionAdapter):
            def submit_entry(self, order, bar):
                raise TimeoutError("status tidak diketahui")
        bars = flat_bars(14, HOUR)
        eng = make_engine(base_cfg("1h"), {2: (1.0, 50.0), 6: (1.0, 50.0)}, bars, adapter_cls=Flaky)
        run_all(eng, bars)
        self.assertEqual(eng.machine.state, State.RECONCILING)
        self.assertFalse(eng.machine.new_entries_allowed)
        self.assertEqual(eng.counters["entries"], 0)

    def test_illegal_transition_halts_fail_closed(self):
        bars = flat_bars(10, HOUR)
        eng = make_engine(base_cfg("1h"), {2: (1.0, 50.0)}, bars)
        orig = eng.machine.transition

        def bad(to, reason="", ts=None):
            if to == State.EXECUTING:
                raise IllegalTransition("diinjeksi")
            return orig(to, reason, ts)
        eng.machine.transition = bad
        run_all(eng, bars)
        self.assertTrue(eng.risk.halted)
        self.assertTrue(eng.risk.halt_reason.startswith("state_machine_illegal"))
        self.assertEqual(eng.counters["entries"], 0)

    def test_kill_switch_becomes_machine_failure_and_blocks_then_clears(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = replace(base_cfg("1h"), state_dir=d)
            bars = flat_bars(30, HOUR)
            eng = make_engine(cfg, {2: (1.0, 50.0), 20: (1.0, 50.0)}, bars)
            open(cfg.kill_switch_file, "w").close()
            for i in range(10):
                eng.process_bar(bars, i)
            self.assertIn(FailureKind.KILL_SWITCH, eng.machine.active_failures)
            self.assertEqual(eng.counters["entries"], 0)
            os.remove(cfg.kill_switch_file)
            for i in range(10, 30):
                eng.process_bar(bars, i)
            self.assertEqual(eng.machine.active_failures, ())
            self.assertEqual(eng.counters["entries"], 1)


class StateMachinePersistenceTests(PaperCase):
    def _open_position(self):
        return self.run_until(lambda r: r["position"] is not None)

    def test_machine_state_is_persisted_and_restored_with_open_position(self):
        k, _ = self._open_position()
        self.assertEqual(self.state()["machine_state"], "POSITION")
        self.step(k)                                   # "restart"
        self.assertEqual(self.state()["machine_state"], "POSITION")

    def test_inconsistent_machine_state_is_refused(self):
        k, _ = self._open_position()
        st = self.state()
        st["machine_state"] = "READY"                 # posisi ada tetapi mesin bilang READY
        with open(self.path("paper_state.json"), "w") as fh:
            import json
            json.dump(st, fh)
        before = self.digest()
        self.assertEqual(self.step(k + 1)["status"], "state_inconsistent")
        self.assertEqual(self.digest(), before)

    def test_position_state_without_position_is_refused(self):
        for k in range(700, 704):
            self.step(k)
        st = self.state()
        self.assertIsNone(st["ledger"]["position"])
        st["machine_state"] = "POSITION"
        with open(self.path("paper_state.json"), "w") as fh:
            import json
            json.dump(st, fh)
        self.assertEqual(self.step(704)["status"], "state_inconsistent")

    def test_legacy_v4_state_without_machine_state_is_accepted(self):
        k, _ = self._open_position()
        st = self.state()
        del st["machine_state"]
        with open(self.path("paper_state.json"), "w") as fh:
            import json
            json.dump(st, fh)
        self.assertEqual(self.step(k + 1)["status"], "ok")
        self.assertEqual(self.state()["machine_state"] in ("POSITION", "READY", "SIGNAL"), True)

    def test_reconciling_survives_restart_and_recovery_returns_to_ready(self):
        for k in range(700, 704):
            self.step(k)
        st = self.state()
        st["machine_state"] = "RECONCILING"
        st["halted"], st["halt_reason"] = True, "exec_unknown:TimeoutError"
        with open(self.path("paper_state.json"), "w") as fh:
            import json
            json.dump(st, fh)
        self.step(704)
        self.assertEqual(self.state()["machine_state"], "RECONCILING")
        recover(self.cfg, "operator", "status order diperiksa di bursa", True)
        self.assertEqual(self.state()["machine_state"], "READY")


class CooldownAnchoringTests(unittest.TestCase):
    def test_cooldown_starts_when_third_loss_closes_not_at_next_signal(self):
        sg = SafeguardExecutionEngine(max_loss_streak=3, cooldown_sec=3600)
        t = 10_000_000_000
        for i in range(3):
            sg.record_closed_trade(-1.0, t + i * 1000, cooldown_anchor_ms=t + i * 1000 + 900_000)
        anchor_s = (t + 2000 + 900_000) / 1000.0
        self.assertEqual(sg.check_circuit_breaker(now=anchor_s + 3599).reason, "cooldown")
        self.assertTrue(sg.check_circuit_breaker(now=anchor_s + 3600).allowed)
        self.assertEqual(sg.loss_streak, 0)

    def test_late_first_check_does_not_extend_cooldown(self):
        sg = SafeguardExecutionEngine(max_loss_streak=3, cooldown_sec=3600)
        for i in range(3):
            sg.record_closed_trade(-1.0, 1000 + i)
        g = sg.check_circuit_breaker(now=10_000.0)    # pertama kali dicek jauh setelah cooldown lewat
        self.assertTrue(g.allowed)
        self.assertNotEqual(g.reason, "loss_streak_cooldown_started")

    def test_engine_scenario_independent_of_intervening_rejected_signals(self):
        bars = flat_bars(30, HOUR)
        for i in (3, 7, 11):                          # tiga rugi beruntun (stop kena di candle entry)
            with_bar(bars, i, 100, 100.05, 98.5, 98.8)
        plan = {2: (1.0, 50.0), 6: (1.0, 50.0), 10: (1.0, 50.0),
                12: (1.0, -1.0),                      # TP di sisi salah -> ditolak Risk (jangan memulai cooldown)
                14: (1.0, 50.0),                      # entry di T15 < akhir cooldown T16 -> ditolak cooldown
                15: (1.0, 50.0)}                      # entry di T16 = akhir cooldown -> BOLEH
        eng = make_engine(base_cfg("1h"), plan, bars)
        run_all(eng, bars)
        self.assertEqual(eng.counters["risk_reject:tp_di_sisi_salah"], 1)
        self.assertEqual(eng.counters["safeguard_reject:cooldown"], 1)
        self.assertEqual(eng.counters["entries"], 4)
        self.assertEqual(eng.ledger.trades[-1]["entry_ts"], bars[16].ts)


class RiskLimitTests(unittest.TestCase):
    def _losing_bars(self, n, crash_at):
        bars = flat_bars(n, HOUR)
        for i in crash_at:
            with_bar(bars, i, 100, 100.05, 98.5, 98.8)
        return bars

    def test_loss_per_trade_is_bounded_by_risk_budget(self):
        bars = self._losing_bars(10, (3,))
        eng = make_engine(base_cfg("1h"), {2: (1.0, 50.0)}, bars)
        run_all(eng, bars)
        t = eng.ledger.trades[0]
        loss = -t["net_pnl"]
        budget = eng.cfg.initial_equity * eng.cfg.risk.risk_per_trade_pct / 100.0
        self.assertEqual(t["exit_reason"], "stop")
        self.assertLessEqual(loss, budget * 1.001, "rugi di stop melebihi anggaran risiko per transaksi")
        self.assertGreater(loss, budget * 0.9, "ukuran posisi jauh di bawah anggaran: sizing tidak efisien/salah")

    def test_position_never_exceeds_notional_cap(self):
        bars = flat_bars(10, HOUR)
        eng = make_engine(base_cfg("1h"), {2: (0.06, 50.0)}, bars)   # stop sangat dekat -> qty mentah sangat besar
        for i in range(5):
            eng.process_bar(bars, i)
        pos = eng.ledger.position
        self.assertIsNotNone(pos)
        self.assertLessEqual(pos.qty * pos.entry_price, eng.cfg.initial_equity * eng.cfg.risk.max_notional_pct_of_equity / 100 * 1.001)

    def test_daily_loss_limit_blocks_further_entries_same_day(self):
        # 2 rugi = ~0,998% dari ekuitas >= batas 0,9% (batas 1,0% persis akan bergantung pada pembulatan lot)
        bars = self._losing_bars(30, (3, 7, 11, 15))
        eng = make_engine(base_cfg("1h", max_loss_streak=10, max_daily_loss_pct=0.9),
                          {2: (1.0, 50.0), 6: (1.0, 50.0), 10: (1.0, 50.0), 14: (1.0, 50.0)}, bars)
        run_all(eng, bars)
        self.assertEqual(eng.counters["entries"], 2)
        self.assertEqual(eng.counters["risk_reject:batas_rugi_harian"], 2)

    def test_drawdown_limit_latches_halt_and_stops_everything(self):
        bars = self._losing_bars(30, (3, 7, 11, 15))
        eng = make_engine(base_cfg("1h", max_loss_streak=10, max_daily_loss_pct=10.0, max_drawdown_pct=0.9),
                          {2: (1.0, 50.0), 6: (1.0, 50.0), 10: (1.0, 50.0), 14: (1.0, 50.0)}, bars)
        run_all(eng, bars)
        self.assertTrue(eng.risk.halted)
        self.assertEqual(eng.counters["entries"], 2)
        self.assertEqual(eng.counters["risk_reject:max_drawdown_halt"], 1)


class InvalidStopTakeProfitTests(unittest.TestCase):
    CASES = {
        "stop_di_atas_entry": ((-1.0, 50.0), "stop_di_sisi_salah"),
        "stop_sama_dengan_entry": ((0.0, 50.0), "stop_di_sisi_salah"),
        "tp_di_bawah_entry": ((1.0, -2.0), "tp_di_sisi_salah"),
        "stop_terlalu_jauh": ((9.0, 50.0), "jarak_stop_di_luar_batas"),
        "stop_terlalu_dekat": ((0.01, 50.0), "jarak_stop_di_luar_batas"),
    }

    def test_invalid_levels_are_vetoed_with_specific_reason(self):
        for name, (levels, reason) in self.CASES.items():
            with self.subTest(name):
                bars = flat_bars(10, HOUR)
                eng = make_engine(base_cfg("1h"), {2: levels}, bars)
                run_all(eng, bars)
                self.assertEqual(eng.counters["entries"], 0)
                self.assertEqual(eng.counters["risk_reject:" + reason], 1, dict(eng.counters))

    def test_non_finite_or_missing_stop_is_vetoed(self):
        class Bad(StubStrategy):
            def signal_at(self, i):
                c = self._c[i]
                if i == 2:
                    return Signal(c.ts, Side.LONG, "bad", c.close, self.val, c.close * 1.1, None, "x/1", "fp")
                return Signal(c.ts, Side.NO_TRADE, "none", c.close, None, None, None, "x/1", "fp")
        for val in (float("nan"), float("inf"), None):
            with self.subTest(val=val):
                bars = flat_bars(10, HOUR)
                eng = make_engine(base_cfg("1h"), {}, bars)
                strat = Bad({})
                strat.val = val
                strat.prepare(bars)
                eng.strategy = strat
                run_all(eng, bars)
                self.assertEqual(eng.counters["entries"], 0)
                self.assertTrue(any(k.startswith("risk_reject:") for k in eng.counters), dict(eng.counters))


if __name__ == "__main__":
    unittest.main()
