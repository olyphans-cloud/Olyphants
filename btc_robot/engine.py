"""Mesin pemroses candle: dipakai identik oleh backtest dan paper (Dok 01 bagian 3 D→H).

Urutan per candle i (tanpa look-ahead), dipecah dua fase agar simulasi multi-mode kausal:
  begin_bar (saat OPEN candle i)
    1. Sinyal tertunda dari penutupan candle i-1 melewati PIPELINE ORDER (satu-satunya jalan ke eksekusi):
         HALT -> Risk Engine -> Safe Guard (breaker, spread, batas/jam) -> gerbang portofolio -> HALT (final) -> eksekusi.
       Setiap tahap hanya bisa MENOLAK; hanya Risk Engine yang bisa menerbitkan ApprovedOrder (ditandatangani).
  end_bar (saat CLOSE candle i)
    2. Cek stop/TP pada candle i (pesimistis).
    3. Manajemen posisi: batas waktu posisi (max_hold) lalu trailing stop (berlaku mulai candle i+1).
    4. Mark-to-market di close, update puncak ekuitas.
    5. Strategi membuat sinyal dari penutupan candle i -> jadi sinyal tertunda.
process_bar() = begin_bar() + end_bar() (hasil identik dengan versi sebelumnya).
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .audit import AuditLog
from .config import Config, Mode
from .execution import ExecutionRejected, SimulatedExecutionAdapter
from .ledger import Ledger, LedgerError
from .models import ApprovedOrder, BookQuote, Candle, Side, Signal
from .statemachine import FailureKind, IllegalTransition, State, StateMachine
from .risk import RiskEngine
from .safeguard import SafeguardExecutionEngine
from .strategy import EmaRsiTrendStrategy
from .timecontract import make_decision_id


PIPELINE = ("halt", "risk", "safeguard", "portfolio", "halt_final", "execute")  # urutan WAJIB (diuji)
QUIET = frozenset({(State.READY, State.ANALYZING), (State.ANALYZING, State.READY)})


def signal_to_dict(s: Signal) -> Dict[str, Any]:
    d = dict(s.__dict__)
    d["side"] = s.side.value
    return d


def signal_from_dict(d: Dict[str, Any]) -> Signal:
    d = dict(d)
    d["side"] = Side(d["side"])
    return Signal(**d)


class Engine:
    def __init__(self, cfg: Config, mode: Mode, strategy: EmaRsiTrendStrategy, risk: RiskEngine,
                 adapter: SimulatedExecutionAdapter, ledger: Ledger, audit: AuditLog, notifier=None,
                 safeguard: Optional[SafeguardExecutionEngine] = None,
                 spread_provider: Optional[Callable[[Candle], Optional[BookQuote]]] = None,
                 entry_gate: Optional[Callable[[ApprovedOrder, Candle], Tuple[bool, str]]] = None,
                 machine_state: Optional[str] = None):
        self.cfg, self.mode = cfg, mode
        self.strategy, self.risk, self.adapter = strategy, risk, adapter
        self.ledger, self.audit = ledger, audit
        self.safeguard = safeguard or SafeguardExecutionEngine(
            max_spread_pct=cfg.risk.max_spread_pct,
            max_loss_streak=cfg.risk.max_loss_streak,
            cooldown_sec=cfg.risk.loss_streak_cooldown_sec,
            max_orderbook_age_sec=cfg.risk.max_orderbook_age_sec)
        self.notifier = notifier
        self.spread_provider = spread_provider
        self.entry_gate = entry_gate
        self.pending: Optional[Signal] = None
        self.counters: Counter = Counter()
        self.equity_curve: list = []
        self.last_entry_hour: Optional[int] = None
        self.entries_in_hour: int = 0
        self.machine = StateMachine(audit, quiet_pairs=QUIET)
        if machine_state is None:  # sesi baru: INIT -> SYNCING -> READY
            self.machine.transition(State.SYNCING, "session_start")
            self.machine.transition(State.READY, "session_ready")
        else:
            self.machine.restore(State(machine_state), "from_saved_state")

    @staticmethod
    def expected_machine_states(has_position: bool, has_pending: bool) -> frozenset:
        """State machine yang konsisten dengan ledger/pending yang tersimpan (dipakai saat memuat state)."""
        if has_position:
            return frozenset({State.POSITION, State.EXITING, State.RECONCILING})
        if has_pending:
            return frozenset({State.SIGNAL})
        return frozenset({State.READY, State.RECONCILING, State.SAFE})

    def _mt(self, to: State, reason: str, ts: int) -> bool:
        """Transisi state machine; transisi ilegal = bug/kerusakan state => HALT (fail-closed)."""
        try:
            self.machine.transition(to, reason, ts)
            return True
        except IllegalTransition as exc:
            self.risk.halt(f"state_machine_illegal:{self.machine.state.value}->{to.value}")
            self.counters["state_machine_illegal"] += 1
            self.audit.log("state_machine_illegal", ts, error=str(exc))
            return False

    # ---- API lama: satu candle utuh ------------------------------------------------------
    def process_bar(self, candles: Sequence[Candle], i: int, data_ok: bool = True) -> None:
        self.begin_bar(candles[i], data_ok)
        self.end_bar(candles, i, data_ok)

    # ---- fase OPEN ------------------------------------------------------------------------
    def begin_bar(self, bar: Candle, data_ok: bool = True) -> None:
        self.ledger.roll_day(bar.ts, bar.open)
        failures = []
        if self.risk._kill_file_present():
            failures.append(FailureKind.KILL_SWITCH)
        if not data_ok:
            failures.append(FailureKind.STALE_OR_MISSING_DATA)
        if tuple(sorted(set(failures), key=lambda k: k.value)) != self.machine.active_failures:
            self.machine.update_failures(failures, bar.ts)
        if self.pending is not None:
            sig, self.pending = self.pending, None
            self._try_enter(sig, bar, data_ok)

    # ---- fase CLOSE -----------------------------------------------------------------------
    def end_bar(self, candles: Sequence[Candle], i: int, data_ok: bool = True) -> None:
        bar = candles[i]
        led = self.ledger

        if led.position is not None and data_ok:
            fill = self.adapter.check_exit(led.position, bar)
            if fill is not None:
                self._record_exit(fill, bar.ts)
            else:
                self._manage_position(bar)

        led.update_peak(bar.close)
        self.equity_curve.append((bar.ts, led.equity(bar.close)))

        if not data_ok:
            self.counters["bar_data_tidak_valid"] += 1
            self.audit.log("data_invalid_skip", bar.ts)
            return
        if self.machine.state == State.READY and self.machine.new_entries_allowed:
            self._mt(State.ANALYZING, "new_bar", bar.ts)
        sig = self.strategy.signal_at(i)
        if sig.side == Side.NO_TRADE:
            self.counters["no_trade:" + sig.reason] += 1
            if self.machine.state == State.ANALYZING:
                self._mt(State.READY, "no_trade", bar.ts)
            return
        self.counters["signals"] += 1
        self.audit.log("signal", bar.ts, decision_id=self._decision_id(sig), signal=signal_to_dict(sig))
        self._notify("send_signal", sig.side.value, sig.ref_price, sig.reason)
        if self.machine.state == State.ANALYZING:
            if led.position is None and not self.risk.halted and self._mt(State.SIGNAL, "signal", bar.ts):
                self.pending = sig
            elif self.machine.state == State.ANALYZING:
                self._mt(State.READY, "signal_not_actionable", bar.ts)

    def _record_exit(self, fill, ts: int) -> None:
        self._mt(State.EXITING, "exit_" + fill.reason, ts)
        trade = self.ledger.apply_exit(fill)
        self.counters["exits_" + fill.reason] += 1
        self.safeguard.record_closed_trade(float(trade.get("net_pnl", 0.0)), ts,
                                           cooldown_anchor_ms=ts + self.cfg.interval_ms,
                                           trade_id=trade.get("client_order_id"))
        self.audit.log("exit", ts, fill=fill.__dict__ | {"side": fill.side.value}, trade=trade)
        self._notify("send_trade_close", trade.get("side", fill.side.value), trade.get("entry_price", fill.price),
                     fill.price, trade.get("net_pnl", 0.0), fill.reason, trade.get("fees"))
        # rekonsiliasi (simulasi): ledger harus datar sebelum kembali READY
        self._mt(State.RECONCILING, "exit_filled", ts)
        if self.ledger.position is None:
            self._mt(State.READY, "reconciled_flat", ts)
        else:  # tidak seharusnya terjadi: jangan lanjut trading
            self.risk.halt("reconcile_mismatch_after_exit")

    def _manage_position(self, bar: Candle) -> None:
        """Batas waktu posisi lalu trailing stop. Error apa pun => halt (fail-closed)."""
        r = self.cfg.risk
        if r.max_hold_sec <= 0 and r.trailing_trail_pct <= 0:
            return
        pos = self.ledger.position
        if pos is None:
            return
        try:
            close_ms = bar.ts + self.cfg.interval_ms
            if r.max_hold_sec > 0 and self.safeguard.evaluate_max_hold_limit(
                    pos.entry_ts / 1000.0, r.max_hold_sec, now=close_ms / 1000.0):
                fill = self.adapter.close_at(pos, bar.close, bar.ts, "max_hold")
                self._record_exit(fill, bar.ts)
                return
            if r.trailing_trail_pct > 0:
                new_stop = self.safeguard.update_dynamic_trailing_stop(
                    bar.close, pos.entry_price, pos.stop_price, r.trailing_trail_pct,
                    pos.side.value, r.trailing_activation_pct)
                if new_stop != pos.stop_price:
                    self.audit.log("trailing_stop_update", bar.ts, old=pos.stop_price, new=new_stop, close=bar.close)
                    pos.stop_price = new_stop
                    self.counters["trailing_updates"] += 1
        except Exception as exc:  # noqa: BLE001 - fail-closed
            self.risk.halt(f"position_mgmt_error:{type(exc).__name__}")
            self.counters["position_mgmt_error"] += 1
            self.audit.log("position_mgmt_error", bar.ts, error=str(exc))

    def _decision_id(self, sig: Signal) -> str:
        return make_decision_id(sig.strategy_version, sig.bar_ts, sig.data_fingerprint, self.audit.config_hash)

    def _spread_gate(self, bar: Candle) -> Optional[str]:
        """None = lolos; selain itu alasan penolakan. Hanya PAPER/LIVE (backtest tidak punya order book)."""
        if not self.cfg.risk.require_spread_check or self.mode == Mode.BACKTEST:
            return None
        quote = None
        if self.spread_provider is not None:
            try:
                quote = self.spread_provider(bar)
            except Exception:  # noqa: BLE001 - sumber data gagal = spread tidak diketahui
                quote = None
        if quote is None:
            return "spread_unavailable"
        gate = self.safeguard.validate_spread_gate(quote.bid, quote.ask, quote.observed_at, quote.now)
        if not gate.allowed:
            self.audit.log("spread_reject", bar.ts, reason=gate.reason, details=gate.details)
            return gate.reason
        return None

    def _halt_reason(self) -> Optional[str]:
        """Pemeriksaan HALT (dipakai dua kali: sebelum risk dan tepat sebelum eksekusi)."""
        if self.risk.halted:
            return f"risk_halt:{self.risk.halt_reason}"
        if self.safeguard.halted:
            return f"safeguard_halt:{self.safeguard.halt_reason}"
        if not self.machine.new_entries_allowed:
            return f"state_machine:{self.machine.state.value}"
        return None

    def _final_halt_reason(self) -> Optional[str]:
        """Pemeriksaan HALT terakhir, tepat sebelum submit (menangkap halt/kill switch yang muncul di tahap tengah)."""
        if self.risk.halted:
            return "risk_halt"
        if self.safeguard.halted:
            return "safeguard_halt"
        if self.risk._kill_file_present():
            return "kill_switch"
        return None

    def _reject_back_to_ready(self, bar: Candle) -> None:
        if self.machine.state in (State.SIGNAL, State.RISK_CHECK):
            if self.machine.state == State.SIGNAL:
                self._mt(State.RISK_CHECK, "evaluate", bar.ts)
            self._mt(State.READY, "rejected", bar.ts)

    def _try_enter(self, sig: Signal, bar: Candle, data_ok: bool) -> None:
        """PIPELINE ORDER (urutan wajib, lihat PIPELINE): halt -> risk -> safeguard -> portfolio -> halt_final -> execute."""
        led = self.ledger
        # --- 1. HALT (paling awal, fail-closed) ---------------------------------------------------
        why = self._halt_reason()
        if why is not None:
            self.counters["halt_reject:" + why.split(":")[0]] += 1
            self.audit.log("halt_reject", bar.ts, reason=why)
            self._reject_back_to_ready(bar)
            return
        if not self._mt(State.RISK_CHECK, "evaluate", bar.ts):
            return
        # --- 2. RISK ENGINE (satu-satunya penerbit ApprovedOrder) ------------------------------------
        bars_since = (bar.ts - sig.bar_ts) // self.cfg.interval_ms
        snap = led.snapshot(bar.open, bar.ts)
        decision, order = self.risk.evaluate(sig, bar.open, snap, data_ok, bars_since)
        self.audit.log("risk_decision", bar.ts, decision_id=self._decision_id(sig),
                       approved=decision.approved, reason=decision.reason,
                       qty=decision.qty, checks=decision.checks, signal_bar_ts=sig.bar_ts)
        if not decision.approved or order is None:
            self.counters["risk_reject:" + decision.reason.split(":")[0]] += 1
            if self.risk.halted:
                self._notify("send_halt", self.risk.halt_reason or decision.reason, decision.checks.get("drawdown_pct"))
            self._mt(State.READY, "risk_veto", bar.ts)
            return
        # --- 3. SAFE GUARD (breaker, spread order book, batas entry per jam) ---------------------------
        gate = self.safeguard.check_circuit_breaker(now=bar.ts / 1000.0)
        if not gate.allowed:
            self.counters["safeguard_reject:" + gate.reason] += 1
            self.audit.log("safeguard_reject", bar.ts, reason=gate.reason, details=gate.details)
            self._mt(State.READY, "safeguard_veto", bar.ts)
            return
        why = self._spread_gate(bar)
        if why is not None:
            self.counters["safeguard_reject:spread:" + why] += 1
            self.audit.log("safeguard_reject", bar.ts, reason="spread:" + why)
            self._mt(State.READY, "spread_veto", bar.ts)
            return
        hour_bucket = bar.ts // 3_600_000
        if self.last_entry_hour == hour_bucket and self.entries_in_hour >= self.cfg.risk.max_entries_per_hour:
            self.counters["entry_hour_limit"] += 1
            self.audit.log("safeguard_reject", bar.ts, reason="max_entries_per_hour", signal_bar_ts=sig.bar_ts)
            self._mt(State.READY, "hour_limit_veto", bar.ts)
            return
        # --- 4. gerbang portofolio (opsional) --------------------------------------------------------
        if self.entry_gate is not None:
            try:
                ok, gate_reason = self.entry_gate(order, bar)
            except Exception as exc:  # noqa: BLE001 - gerbang rusak = tolak
                ok, gate_reason = False, f"gate_error:{type(exc).__name__}"
            if not ok:
                self.counters["portfolio_reject:" + gate_reason] += 1
                self.audit.log("portfolio_reject", bar.ts, reason=gate_reason)
                self._mt(State.READY, "portfolio_veto", bar.ts)
                return
        # --- 5. HALT final (tepat sebelum eksekusi) ---------------------------------------------------
        final = self._final_halt_reason()
        if final is not None:
            self.counters["halt_reject:final"] += 1
            self.audit.log("halt_reject", bar.ts, reason="final_check:" + final)
            self._mt(State.READY, "halt_final", bar.ts)
            return
        # --- 6. EKSEKUSI ------------------------------------------------------------------------------
        if not self._mt(State.EXECUTING, "submit", bar.ts):
            return
        try:
            fill = self.adapter.submit_entry(order, bar)
            led.apply_entry(fill, order)
        except (ExecutionRejected, LedgerError) as exc:
            self.counters["exec_error"] += 1
            self.risk.halt(f"exec_error:{exc}")  # fail-safe: kesalahan eksekusi menghentikan entri baru
            self.audit.log("exec_error", bar.ts, error=str(exc))
            self._mt(State.READY, "exec_rejected", bar.ts)
            return
        except Exception as exc:  # noqa: BLE001 - status order TIDAK DIKETAHUI: jangan coba lagi, halt
            self.counters["exec_unknown"] += 1
            self.risk.halt(f"exec_unknown:{type(exc).__name__}")
            self.audit.log("exec_unknown", bar.ts, error=str(exc), order_id=order.client_order_id)
            self._mt(State.RECONCILING, "order_status_unknown", bar.ts)
            return
        self._mt(State.POSITION, "filled", bar.ts)
        self.counters["entries"] += 1
        if self.last_entry_hour != hour_bucket:
            self.last_entry_hour, self.entries_in_hour = hour_bucket, 0
        self.entries_in_hour += 1
        self.audit.log("entry", bar.ts, decision_id=self._decision_id(sig),
                       fill=fill.__dict__ | {"side": fill.side.value}, order_id=order.client_order_id)
        self._notify("send_trade_open", fill.side.value, fill.price, fill.qty, order.stop_price, order.take_profit)

    def _notify(self, method: str, *args) -> None:
        """Notifikasi best-effort: kegagalan Telegram tidak boleh mengubah keputusan trading."""
        if self.notifier is None:
            return
        try:
            getattr(self.notifier, method)(*args)
        except Exception as exc:  # noqa: BLE001
            self.audit.log("notification_error", 0, method=method, error=type(exc).__name__)

    def finalize(self, candles: Sequence[Candle]) -> None:
        """Tutup posisi tersisa di akhir data agar hasil tidak bergantung pada posisi mengambang."""
        if self.ledger.position is not None:
            last = candles[-1]
            fill = self.adapter.close_at(self.ledger.position, last.close, last.ts, "end_of_data")
            self._record_exit(fill, last.ts)
            self.equity_curve[-1] = (last.ts, self.ledger.equity(last.close))
