"""Portfolio ledger: posisi, kas, PnL terealisasi, biaya, rekonsiliasi (Dok 01 bagian 3 G)."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from .models import ApprovedOrder, Fill, PortfolioSnapshot, Position, Side

DAY_MS = 86_400_000


class LedgerError(RuntimeError):
    pass


class Ledger:
    def __init__(self, initial_equity: float):
        self.initial_equity = initial_equity
        self.cash = initial_equity
        self.position: Optional[Position] = None
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.peak_equity = initial_equity
        self.day_key: Optional[int] = None
        self.day_start_equity = initial_equity
        self.day_realized = 0.0
        self.trades: List[Dict[str, Any]] = []

    def equity(self, mark: float) -> float:
        if self.position is None:
            return self.cash
        p = self.position
        return self.cash + p.qty * mark if p.side == Side.LONG else self.cash - p.qty * mark

    def roll_day(self, ts: int, mark: float) -> None:
        key = ts // DAY_MS  # hari UTC
        if key != self.day_key:
            self.day_key = key
            self.day_start_equity = self.equity(mark)
            self.day_realized = 0.0

    def update_peak(self, mark: float) -> None:
        self.peak_equity = max(self.peak_equity, self.equity(mark))

    def apply_entry(self, fill: Fill, order: ApprovedOrder) -> None:
        if self.position is not None:
            raise LedgerError("sudah ada posisi terbuka")
        notional = fill.qty * fill.price
        if fill.side == Side.LONG:
            if self.cash < notional + fill.fee - 1e-9:
                raise LedgerError("kas tidak cukup untuk entry")
            self.cash -= notional + fill.fee
        else:
            self.cash += notional - fill.fee
        self.fees_paid += fill.fee
        self.position = Position(fill.side, fill.qty, fill.price, order.stop_price, order.take_profit,
                                 fill.ts, fill.fee, fill.client_order_id)

    def apply_exit(self, fill: Fill) -> Dict[str, Any]:
        p = self.position
        if p is None or p.client_order_id != fill.client_order_id or abs(p.qty - fill.qty) > 1e-12:
            raise LedgerError("exit tidak cocok dengan posisi terbuka")
        notional = fill.qty * fill.price
        if p.side == Side.LONG:
            self.cash += notional - fill.fee
            gross = (fill.price - p.entry_price) * p.qty
        else:
            self.cash -= notional + fill.fee
            gross = (p.entry_price - fill.price) * p.qty
        net = gross - p.entry_fee - fill.fee
        self.fees_paid += fill.fee
        self.realized_pnl += net
        self.day_realized += net
        trade = {
            "client_order_id": p.client_order_id, "side": p.side.value, "qty": p.qty,
            "entry_ts": p.entry_ts, "entry_price": p.entry_price,
            "exit_ts": fill.ts, "exit_price": fill.price, "exit_reason": fill.reason,
            "gross_pnl": gross, "fees": p.entry_fee + fill.fee, "net_pnl": net,
        }
        self.trades.append(trade)
        self.position = None
        return trade

    def snapshot(self, mark: float, ts: int) -> PortfolioSnapshot:
        return PortfolioSnapshot(ts, self.equity(mark), self.cash, self.peak_equity,
                                 self.day_start_equity, self.day_realized,
                                 1 if self.position else 0)

    def reconcile(self, exchange_qty: float, tol: float = 1e-8) -> bool:
        """True bila qty internal (bertanda: long +, short -) cocok dengan exchange."""
        internal = 0.0
        if self.position:
            internal = self.position.qty if self.position.side == Side.LONG else -self.position.qty
        return abs(internal - exchange_qty) <= tol

    # ---- persistensi (paper) --------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "position"}
        d["position"] = None
        if self.position:
            pd = asdict(self.position)
            pd["side"] = self.position.side.value
            d["position"] = pd
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Ledger":
        led = cls(d["initial_equity"])
        for k, v in d.items():
            if k == "position":
                continue
            setattr(led, k, v)
        if d.get("position"):
            pd = dict(d["position"])
            pd["side"] = Side(pd["side"])
            led.position = Position(**pd)
        return led
