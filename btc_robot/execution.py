"""Adapter eksekusi SIMULASI (backtest/paper). Adapter testnet/live sengaja belum ada.

- Menolak order tanpa tanda tangan risk engine.
- Idempoten terhadap client_order_id (retry tidak menggandakan fill).
- Fill pesimistis: slippage + setengah spread melawan kita, stop didahulukan bila stop & TP kena di candle sama,
  gap melewati stop diisi di harga open.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

from .config import CostConfig, Mode
from .models import ApprovedOrder, Candle, Fill, Position, Side


class ExecutionRejected(RuntimeError):
    pass


class SimulatedExecutionAdapter:
    def __init__(self, costs: CostConfig, mode: Mode, verifier: Callable[[ApprovedOrder], bool]):
        if mode not in (Mode.BACKTEST, Mode.PAPER):
            raise ExecutionRejected(f"adapter simulasi tidak boleh dipakai pada mode {mode.value}")
        self.costs = costs
        self.mode = mode
        self._verify = verifier
        self.seen: Dict[str, Fill] = {}
        self.duplicates_blocked = 0

    # harga bergerak melawan kita
    def _adverse(self, price: float, buying: bool) -> float:
        bps = (self.costs.spread_bps / 2.0 + self.costs.slippage_bps) / 10_000.0
        return price * (1.0 + bps) if buying else price * (1.0 - bps)

    def _fee(self, qty: float, price: float) -> float:
        return qty * price * self.costs.fee_rate

    def submit_entry(self, order: ApprovedOrder, bar: Candle) -> Fill:
        if not isinstance(order, ApprovedOrder) or not self._verify(order):
            raise ExecutionRejected("order tanpa persetujuan risk engine yang valid")
        if order.mode != self.mode.value:
            raise ExecutionRejected("mode order tidak cocok dengan adapter")
        prior = self.seen.get(order.client_order_id)
        if prior is not None:
            self.duplicates_blocked += 1
            return prior
        buying = order.side == Side.LONG
        price = self._adverse(bar.open, buying)
        fill = Fill(order.client_order_id, "ENTRY", order.side, order.qty, price,
                    self._fee(order.qty, price), bar.ts, "entry_at_open")
        self.seen[order.client_order_id] = fill
        return fill

    def check_exit(self, pos: Position, bar: Candle) -> Optional[Fill]:
        long = pos.side == Side.LONG
        stop_hit = bar.low <= pos.stop_price if long else bar.high >= pos.stop_price
        tp_hit = False
        if pos.take_profit is not None:
            tp_hit = bar.high >= pos.take_profit if long else bar.low <= pos.take_profit
        if stop_hit:  # pesimistis: stop menang bila keduanya kena
            raw = min(pos.stop_price, bar.open) if long else max(pos.stop_price, bar.open)
            px = self._adverse(raw, buying=not long)
            return Fill(pos.client_order_id, "EXIT", pos.side, pos.qty, px, self._fee(pos.qty, px),
                        bar.ts, "stop")
        if tp_hit:
            px = pos.take_profit  # limit order: harga tetap, tanpa slippage
            return Fill(pos.client_order_id, "EXIT", pos.side, pos.qty, px, self._fee(pos.qty, px),
                        bar.ts, "take_profit")
        return None

    def close_at(self, pos: Position, price: float, ts: int, reason: str) -> Fill:
        """Penutupan paksa (akhir data/darurat) di harga referensi + biaya."""
        px = self._adverse(price, buying=pos.side != Side.LONG)
        return Fill(pos.client_order_id, "EXIT", pos.side, pos.qty, px, self._fee(pos.qty, px), ts, reason)
