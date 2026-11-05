"""Indikator KAUSAL: nilai di indeks i hanya memakai data indeks <= i. None = warm-up."""
from __future__ import annotations

from typing import List, Optional, Sequence

Series = List[Optional[float]]


def ema(values: Sequence[float], period: int) -> Series:
    n = len(values)
    out: Series = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(values: Sequence[float], period: int = 14) -> Series:
    n = len(values)
    out: Series = [None] * n
    if period < 2 or n <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = _rsi_value(avg_g, avg_l)
    for i in range(period + 1, n):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi_value(avg_g, avg_l)
    return out


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int = 14) -> Series:
    n = len(close)
    out: Series = [None] * n
    if period < 2 or n <= period:
        return out
    tr = [high[0] - low[0]]
    for i in range(1, n):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    prev = sum(tr[1:period + 1]) / period
    out[period] = prev
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out
