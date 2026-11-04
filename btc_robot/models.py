"""Tipe data bersama. Semua timestamp = epoch milidetik UTC (waktu BUKA candle)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    """Usulan dari strategi/AI. Bukan perintah order."""
    bar_ts: int
    side: Side
    reason: str
    ref_price: float
    stop_price: Optional[float]
    take_profit: Optional[float]
    confidence: Optional[float]      # None = belum terkalibrasi
    strategy_version: str
    data_fingerprint: str


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    qty: float = 0.0
    checks: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovedOrder:
    """Hanya dibuat RiskEngine; ditandatangani. Adapter menolak yang tanpa tanda tangan valid."""
    client_order_id: str
    side: Side
    qty: float
    ref_price: float
    stop_price: float
    take_profit: Optional[float]
    signal_bar_ts: int
    mode: str
    signature: str


@dataclass(frozen=True)
class Fill:
    client_order_id: str
    kind: str                # "ENTRY" | "EXIT"
    side: Side               # sisi posisi (LONG/SHORT), bukan arah order
    qty: float
    price: float
    fee: float
    ts: int
    reason: str
    simulated: bool = True


@dataclass
class Position:
    side: Side
    qty: float
    entry_price: float
    stop_price: float
    take_profit: Optional[float]
    entry_ts: int
    entry_fee: float
    client_order_id: str


@dataclass(frozen=True)
class PortfolioSnapshot:
    ts: int
    equity: float
    cash: float
    peak_equity: float
    day_start_equity: float
    day_realized_pnl: float
    open_positions: int


@dataclass(frozen=True)
class BookQuote:
    """Kuotasi order book teratas. observed_at/now = epoch detik; selisihnya = umur data."""
    bid: float
    ask: float
    observed_at: float
    now: float
