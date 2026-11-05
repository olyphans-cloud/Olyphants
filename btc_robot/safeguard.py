"""Fail-closed execution safeguards shared by paper/research adapters.

This module does NOT submit exchange orders. A production adapter must call the gates
before entry and must verify exchange acknowledgements before claiming an exit is complete.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str
    details: Dict[str, Any]


class SafeguardExecutionEngine:
    def __init__(self, max_spread_pct: float = 0.15, max_loss_streak: int = 3,
                 cooldown_sec: int = 300, state_path: Optional[str] = None,
                 max_orderbook_age_sec: float = 5.0):
        if not (math.isfinite(max_spread_pct) and 0 < max_spread_pct <= 5):
            raise ValueError("max_spread_pct harus di (0, 5]")
        if max_loss_streak < 1 or cooldown_sec < 1 or max_orderbook_age_sec <= 0:
            raise ValueError("parameter safeguard tidak valid")
        self.max_spread_pct = float(max_spread_pct)
        self.max_loss_streak = int(max_loss_streak)
        self.cooldown_sec = int(cooldown_sec)
        self.max_orderbook_age_sec = float(max_orderbook_age_sec)
        self.state_path = state_path
        self.loss_streak = 0
        self.cooldown_until = 0.0
        self.last_trade_ts = -1  # kompatibilitas replay state lama
        self.processed_trade_ids = set()
        self.halted = False
        self.halt_reason: Optional[str] = None
        if state_path:
            self._load()

    def _load(self) -> None:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                s = json.load(f)
            if not isinstance(s, dict):
                raise ValueError("state root invalid")
            self.loss_streak = max(0, int(s.get("loss_streak", 0)))
            self.cooldown_until = max(0.0, float(s.get("cooldown_until", 0)))
            self.last_trade_ts = int(s.get("last_trade_ts", -1))
            ids = s.get("processed_trade_ids", [])
            if not isinstance(ids, list) or any(not isinstance(x, str) or not x for x in ids):
                raise ValueError("processed_trade_ids invalid")
            self.processed_trade_ids = set(ids)
            self.halted = bool(s.get("halted", False))
            self.halt_reason = s.get("halt_reason")
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            # Corrupt state must never silently reset the risk controls.
            self.halted, self.halt_reason = True, f"safeguard_state_unreadable:{type(exc).__name__}"

    def _save(self) -> None:
        if not self.state_path:
            return
        directory = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(directory, exist_ok=True)
        payload = {"loss_streak": self.loss_streak, "cooldown_until": self.cooldown_until,
                   "last_trade_ts": self.last_trade_ts,
                   "processed_trade_ids": sorted(self.processed_trade_ids),
                   "halted": self.halted, "halt_reason": self.halt_reason}
        fd, tmp = tempfile.mkstemp(prefix=".safeguard-", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, sort_keys=True)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def validate_spread_gate(self, best_bid: float, best_ask: float,
                             observed_at: Optional[float] = None,
                             now: Optional[float] = None) -> GateResult:
        now = time.time() if now is None else now
        observed_at = now if observed_at is None else observed_at
        values = (best_bid, best_ask, observed_at, now)
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in values):
            return GateResult(False, "orderbook_non_finite", {})
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return GateResult(False, "orderbook_price_invalid", {"bid": best_bid, "ask": best_ask})
        age = now - observed_at
        if age < -1.0 or age > self.max_orderbook_age_sec:
            return GateResult(False, "orderbook_stale_or_future", {"age_sec": age})
        spread_pct = (best_ask - best_bid) / best_bid * 100.0
        if spread_pct > self.max_spread_pct:
            return GateResult(False, "spread_limit", {"spread_pct": spread_pct, "limit_pct": self.max_spread_pct})
        return GateResult(True, "spread_ok", {"spread_pct": spread_pct, "age_sec": age})

    def check_circuit_breaker(self, now: Optional[float] = None) -> GateResult:
        now = time.time() if now is None else now
        if self.halted:
            return GateResult(False, "halt_latched", {"reason": self.halt_reason})
        if now < self.cooldown_until:
            return GateResult(False, "cooldown", {"remaining_sec": self.cooldown_until - now})
        if self.loss_streak >= self.max_loss_streak:
            self.cooldown_until = now + self.cooldown_sec
            self.loss_streak = 0
            self._save()
            return GateResult(False, "loss_streak_cooldown_started", {"cooldown_sec": self.cooldown_sec})
        return GateResult(True, "risk_gate_ok", {"loss_streak": self.loss_streak})

    def record_closed_trade(self, net_pnl: float, closed_ts: Optional[int] = None,
                            cooldown_anchor_ms: Optional[int] = None,
                            trade_id: Optional[str] = None) -> None:
        # ID transaksi menjadi kunci deduplikasi. Timestamp tetap dipakai
        # sebagai fallback untuk pemanggil lama tanpa trade_id.
        if trade_id is not None:
            if not isinstance(trade_id, str) or not trade_id:
                self.halt("invalid_closed_trade_id")
                return
            if trade_id in self.processed_trade_ids:
                return
        elif closed_ts is not None and closed_ts <= self.last_trade_ts:
            return

        if not isinstance(net_pnl, (int, float)) or not math.isfinite(net_pnl):
            self.halt("invalid_closed_trade_pnl")
            return

        if closed_ts is not None:
            self.last_trade_ts = max(self.last_trade_ts, int(closed_ts))

        if net_pnl < 0:
            self.loss_streak += 1
        elif net_pnl > 0:
            self.loss_streak = 0

        # Break-even does not erase a loss streak.
        if closed_ts is not None and self.loss_streak >= self.max_loss_streak:
            anchor = closed_ts if cooldown_anchor_ms is None else cooldown_anchor_ms
            self.cooldown_until = max(
                self.cooldown_until,
                anchor / 1000.0 + self.cooldown_sec,
            )
            self.loss_streak = 0

        if trade_id is not None:
            self.processed_trade_ids.add(trade_id)
        self._save()

    def evaluate_max_hold_limit(self, entry_timestamp: float, max_hold_sec: int = 90,
                                now: Optional[float] = None) -> bool:
        if max_hold_sec <= 0 or not math.isfinite(entry_timestamp):
            raise ValueError("entry_timestamp/max_hold_sec tidak valid")
        now = time.time() if now is None else now
        return now - entry_timestamp >= max_hold_sec

    def update_dynamic_trailing_stop(self, current_price: float, entry_price: float,
                                     current_stop: float, trail_pct: float = 0.85,
                                     side: str = "LONG", activation_pct: float = 1.0) -> float:
        vals = (current_price, entry_price, current_stop, trail_pct, activation_pct)
        if not all(math.isfinite(x) for x in vals) or current_price <= 0 or entry_price <= 0:
            raise ValueError("harga/parameter trailing stop tidak valid")
        if not (0 < trail_pct < 100 and activation_pct >= 0):
            raise ValueError("persentase trailing stop tidak valid")
        side = side.upper()
        if side == "LONG":
            if current_price < entry_price * (1 + activation_pct / 100):
                return current_stop
            return max(current_stop, current_price * (1 - trail_pct / 100))
        if side == "SHORT":
            if current_price > entry_price * (1 - activation_pct / 100):
                return current_stop
            candidate = current_price * (1 + trail_pct / 100)
            return min(current_stop, candidate)
        raise ValueError("side harus LONG atau SHORT")

    def release_halt(self) -> None:
        """Hanya melepas latch HALT. loss_streak dan cooldown TIDAK di-reset (batas risiko tetap berlaku)."""
        self.halted, self.halt_reason = False, None
        self._save()

    def halt(self, reason: str) -> None:
        self.halted, self.halt_reason = True, (reason or "unspecified_halt")
        self._save()

    def status(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        return {"halted": self.halted, "halt_reason": self.halt_reason,
                "loss_streak": self.loss_streak,
                "cooldown_remaining_sec": max(0.0, self.cooldown_until - now),
                "max_spread_pct": self.max_spread_pct}
