"""State machine, presedensi kegagalan, dan mode degradasi (Master Roadmap bagian 9–10).

Presedensi: Safety/Kill Switch > Exchange Reality > Risk > Data Integrity > Strategy/AI > News.
Keluar dari SAFE hanya lewat recover() oleh manusia yang tercatat (human override diaudit).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from .audit import AuditLog


class State(str, Enum):
    INIT = "INIT"
    SYNCING = "SYNCING"
    READY = "READY"
    ANALYZING = "ANALYZING"
    SIGNAL = "SIGNAL"
    RISK_CHECK = "RISK_CHECK"
    EXECUTING = "EXECUTING"
    POSITION = "POSITION"
    EXITING = "EXITING"
    RECONCILING = "RECONCILING"
    SAFE = "SAFE"


S = State
# Tabel transisi eksplisit. Menuju SAFE selalu diizinkan dari state mana pun (presedensi keselamatan).
ALLOWED: Dict[State, FrozenSet[State]] = {
    S.INIT: frozenset({S.SYNCING}),
    S.SYNCING: frozenset({S.READY}),
    S.READY: frozenset({S.ANALYZING, S.RECONCILING}),
    S.ANALYZING: frozenset({S.READY, S.SIGNAL}),                       # READY = WAIT / INSUFFICIENT_EVIDENCE
    S.SIGNAL: frozenset({S.RISK_CHECK, S.READY}),                      # READY = TTL kedaluwarsa
    S.RISK_CHECK: frozenset({S.EXECUTING, S.READY}),                   # READY = veto
    S.EXECUTING: frozenset({S.POSITION, S.RECONCILING, S.READY}),      # RECONCILING = timeout/unknown order
    S.POSITION: frozenset({S.EXITING, S.RECONCILING}),
    S.EXITING: frozenset({S.RECONCILING}),
    S.RECONCILING: frozenset({S.READY, S.POSITION}),
    S.SAFE: frozenset(),                                               # hanya via recover()
}
ENTRY_CAPABLE: FrozenSet[State] = frozenset({S.READY, S.ANALYZING, S.SIGNAL, S.RISK_CHECK})


class IllegalTransition(RuntimeError):
    pass


class FailureKind(str, Enum):
    MARKET_DATA = "MARKET_DATA"
    CLOCK = "CLOCK"
    RISK_ENGINE = "RISK_ENGINE"
    EXCHANGE_API = "EXCHANGE_API"
    NEWS = "NEWS"
    AI = "AI"
    DB_LOGGING = "DB_LOGGING"
    CONFLICTING_DATA = "CONFLICTING_DATA"
    STALE_OR_MISSING_DATA = "STALE_OR_MISSING_DATA"
    KILL_SWITCH = "KILL_SWITCH"


LAYERS = ["SAFETY", "EXCHANGE", "RISK", "DATA", "STRATEGY_AI", "NEWS"]  # indeks kecil = presedensi tinggi


@dataclass(frozen=True)
class Response:
    layer: str
    block_new_entries: bool = False
    go_safe: bool = False
    reject_decision: bool = False
    reconcile: bool = False
    degraded: bool = False
    fallback_or_safe: bool = False
    emergency_handling: bool = False
    note: str = ""


RESPONSES: Dict[FailureKind, Response] = {
    FailureKind.MARKET_DATA: Response("DATA", block_new_entries=True, go_safe=True, note="Stop new trade / SAFE"),
    FailureKind.CLOCK: Response("DATA", block_new_entries=True, go_safe=True, note="Stop new trade / SAFE"),
    FailureKind.RISK_ENGINE: Response("RISK", block_new_entries=True, note="Stop new trade"),
    FailureKind.EXCHANGE_API: Response("EXCHANGE", block_new_entries=True, reconcile=True,
                                       note="Stop entry; reconcile/recover"),
    FailureKind.NEWS: Response("NEWS", degraded=True,
                               note="Degraded; strategi yang butuh news diblok sesuai dependency"),
    FailureKind.AI: Response("STRATEGY_AI", fallback_or_safe=True, note="Deterministic fallback atau SAFE"),
    FailureKind.DB_LOGGING: Response("DATA", go_safe=True, block_new_entries=True,
                                     note="Controlled safe mode"),
    FailureKind.CONFLICTING_DATA: Response("DATA", reject_decision=True, note="Reject affected decision"),
    FailureKind.STALE_OR_MISSING_DATA: Response("DATA", reject_decision=True, note="Reject affected decision"),
    FailureKind.KILL_SWITCH: Response("SAFETY", block_new_entries=True, emergency_handling=True,
                                      note="No new entry; emergency handling sesuai level"),
}


def resolve(kinds: Iterable[FailureKind]) -> Optional[Response]:
    """Gabungkan kegagalan aktif: layer = presedensi tertinggi; flag di-OR (selalu ke arah lebih aman)."""
    rs = [RESPONSES[k] for k in kinds]
    if not rs:
        return None
    top = min(rs, key=lambda r: LAYERS.index(r.layer))
    return Response(
        layer=top.layer,
        block_new_entries=any(r.block_new_entries for r in rs),
        go_safe=any(r.go_safe for r in rs),
        reject_decision=any(r.reject_decision for r in rs),
        reconcile=any(r.reconcile for r in rs),
        degraded=any(r.degraded for r in rs),
        fallback_or_safe=any(r.fallback_or_safe for r in rs),
        emergency_handling=any(r.emergency_handling for r in rs),
        note="; ".join(sorted({r.note for r in rs})),
    )


class StateMachine:
    HISTORY_MAX = 2000

    def __init__(self, audit: Optional[AuditLog] = None,
                 quiet_pairs: FrozenSet[Tuple[State, State]] = frozenset()):
        self.state = State.INIT
        self.audit = audit
        self.quiet_pairs = quiet_pairs          # transisi rutin yang tidak ditulis ke audit (hindari banjir log)
        self.active_failures: Tuple[FailureKind, ...] = ()
        self.history: List[Tuple[State, State, str]] = []

    def _log(self, event: str, ts: Optional[int], **kw) -> None:
        if self.audit is not None:
            self.audit.log(event, ts, **kw)

    def _set(self, to: State, reason: str, ts: Optional[int]) -> None:
        self.history.append((self.state, to, reason))
        if len(self.history) > self.HISTORY_MAX:
            del self.history[: self.HISTORY_MAX // 2]
        if (self.state, to) not in self.quiet_pairs:
            self._log("state_transition", ts, frm=self.state.value, to=to.value, reason=reason)
        self.state = to

    def restore(self, state: State, reason: str = "restore", ts: Optional[int] = None) -> None:
        """Pulihkan state dari penyimpanan (hanya dari INIT). Konsistensi dengan ledger diperiksa pemanggil."""
        if self.state != State.INIT:
            raise IllegalTransition("restore() hanya dari INIT")
        self._log("state_restored", ts, to=state.value, reason=reason)
        self.state = state

    def transition(self, to: State, reason: str, ts: Optional[int] = None) -> None:
        if to == self.state:
            return
        if to == State.SAFE:
            self._set(to, reason, ts)
            return
        if to not in ALLOWED[self.state]:
            self._log("illegal_transition_blocked", ts, frm=self.state.value, to=to.value)
            raise IllegalTransition(f"{self.state.value} -> {to.value} tidak diizinkan")
        if to == State.ANALYZING and not self.new_entries_allowed:
            raise IllegalTransition("analisis entri diblokir oleh kegagalan aktif")
        self._set(to, reason, ts)

    def update_failures(self, kinds: Iterable[FailureKind], ts: Optional[int] = None) -> Optional[Response]:
        self.active_failures = tuple(sorted(set(kinds), key=lambda k: k.value))
        resp = resolve(self.active_failures)
        self._log("failures_updated", ts, kinds=[k.value for k in self.active_failures],
                  response=None if resp is None else resp.__dict__)
        if resp is not None and resp.go_safe and self.state != State.SAFE:
            self._set(State.SAFE, "failure:" + ",".join(k.value for k in self.active_failures), ts)
        return resp

    def recover(self, approved_by: str, reason: str, ts: Optional[int] = None) -> None:
        """Satu-satunya jalan keluar dari SAFE; wajib identitas manusia, dan tidak boleh bila kegagalan masih aktif."""
        if self.state != State.SAFE:
            raise IllegalTransition("recover() hanya dari SAFE")
        if not approved_by or not approved_by.strip():
            raise IllegalTransition("recover() butuh approved_by (human override diaudit)")
        resp = resolve(self.active_failures)
        if resp is not None and resp.go_safe:
            raise IllegalTransition("kegagalan penyebab SAFE masih aktif")
        self._log("human_override_recover", ts, approved_by=approved_by, reason=reason)
        self._set(State.SYNCING, f"recover:{reason}", ts)

    @property
    def new_entries_allowed(self) -> bool:
        resp = resolve(self.active_failures)
        blocked = resp is not None and (resp.block_new_entries or resp.go_safe or resp.emergency_handling)
        return self.state in ENTRY_CAPABLE and not blocked
