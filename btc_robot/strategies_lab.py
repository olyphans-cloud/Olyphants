"""Strategi laboratorium untuk M15/H1 (satu strategi = satu hipotesis, parameter TETAP).

Aturan arsitektur sama dengan strategy.py: hanya menghasilkan Signal, tidak punya jalur ke order,
dan sinyal di indeks i hanya memakai data indeks <= i (kausal). Parameter sengaja dikunci pada nilai
konvensional (bukan hasil optimasi) supaya hasil uji tidak sekadar overfit ke data.

Mode: lab_pullback, lab_fib, lab_donchian, lab_bbrev (boleh dipakai di interval 15m maupun 1h).
Semua memakai filter tren EMA200 pada timeframe yang sama dan stop berbasis ATR.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .config import StrategyConfig
from .indicators import ema
from .models import Candle, Side, Signal
from .strategy import EmaRsiTrendStrategy

TREND_EMA = 200


def _roll_max(vals: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        out[i] = max(vals[i - n + 1:i + 1])
    return out


def _roll_min(vals: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        out[i] = min(vals[i - n + 1:i + 1])
    return out


class _LabBase(EmaRsiTrendStrategy):
    VERSION = "lab_base/1.0.0"
    MIN_RR = 1.2

    def prepare(self, candles: Sequence[Candle]) -> None:
        super().prepare(candles)
        closes = [c.close for c in candles]
        self._trend = ema(closes, TREND_EMA)

    def _trend_dir(self, i: int) -> int:
        t = self._trend[i]
        if t is None:
            return 0
        c = self._candles[i].close
        return 1 if c > t else -1

    def _mk(self, i: int, side: Side, reason: str, stop: float, tp: float) -> Signal:
        c = self._candles[i]
        risk = abs(c.close - stop)
        reward = abs(tp - c.close)
        if risk <= 0 or reward / risk < self.MIN_RR:
            return self._no(i, "rr_rendah")
        return Signal(c.ts, side, reason, c.close, stop, tp, None, self.VERSION, self._fp)


class PullbackStrategy(_LabBase):
    """Lanjutan tren: harga > EMA200, EMA20 > EMA50, harga menyentuh EMA20 lalu ditutup kembali di atasnya."""
    VERSION = "lab_pullback/1.0.0"

    def signal_at(self, i: int) -> Signal:
        if i < 2 or self._fast[i] is None or self._slow[i] is None or self._atr[i] is None \
                or self._trend[i] is None or self._rsi[i] is None:
            return self._no(i, "warmup")
        c, p = self._candles[i], self._candles[i - 1]
        f, s, a, r = self._fast[i], self._slow[i], self._atr[i], self._rsi[i]
        d = self._trend_dir(i)
        rr = self.cfg.reward_risk
        sd = self.cfg.atr_stop_mult * a
        if d > 0 and f > s and c.low <= f and c.close > f and c.close > c.open and 40 <= r <= 70:
            return self._mk(i, Side.LONG, f"pullback_up rsi={r:.1f}", c.close - sd, c.close + rr * sd)
        if d < 0 and f < s and c.high >= f and c.close < f and c.close < c.open and 30 <= r <= 60:
            return self._mk(i, Side.SHORT, f"pullback_down rsi={r:.1f}", c.close + sd, c.close - rr * sd)
        return self._no(i, "tidak_ada_setup")


class FibStrategy(_LabBase):
    """Fibonacci retracement searah tren EMA200.

    Leg naik: swing low (terendah 60 candle) terjadi SEBELUM swing high (tertinggi 60 candle) dan tinggi
    leg >= 3 ATR. Entry LONG bila candle bullish menyentuh zona 0.382-0.618 dan ditutup di atas 0.5-nya.
    Stop di bawah level 0.786 (- 0.25 ATR), TP di swing high (retest). SHORT adalah cerminannya.
    """
    VERSION = "lab_fib/1.0.0"
    LOOK = 60
    MIN_LEG_ATR = 3.0

    def prepare(self, candles: Sequence[Candle]) -> None:
        super().prepare(candles)
        self._hh = _roll_max([c.high for c in candles], self.LOOK)
        self._ll = _roll_min([c.low for c in candles], self.LOOK)

    def _leg_indexes(self, i: int):
        lo_i = hi_i = i
        hh, ll = self._hh[i], self._ll[i]
        base = i - self.LOOK + 1
        for k in range(i, base - 1, -1):
            if hi_i == i and self._candles[k].high == hh:
                hi_i = k
            if lo_i == i and self._candles[k].low == ll:
                lo_i = k
        return lo_i, hi_i

    def signal_at(self, i: int) -> Signal:
        if i < self.LOOK or self._hh[i] is None or self._atr[i] is None or self._trend[i] is None:
            return self._no(i, "warmup")
        c = self._candles[i]
        a = self._atr[i]
        hh, ll = self._hh[i], self._ll[i]
        leg = hh - ll
        if a <= 0 or leg < self.MIN_LEG_ATR * a:
            return self._no(i, "leg_kecil")
        lo_i, hi_i = self._leg_indexes(i)
        d = self._trend_dir(i)
        if d > 0 and lo_i < hi_i < i:
            z382, z50, z618, z786 = (hh - leg * x for x in (0.382, 0.5, 0.618, 0.786))
            if c.low <= z382 and c.low >= z786 and c.close > c.open and c.close > z50:
                return self._mk(i, Side.LONG, f"fib_long leg={leg / a:.1f}atr", z786 - 0.25 * a, hh)
        if d < 0 and hi_i < lo_i < i:
            z382, z50, z618, z786 = (ll + leg * x for x in (0.382, 0.5, 0.618, 0.786))
            if c.high >= z382 and c.high <= z786 and c.close < c.open and c.close < z50:
                return self._mk(i, Side.SHORT, f"fib_short leg={leg / a:.1f}atr", z786 + 0.25 * a, ll)
        return self._no(i, "tidak_ada_setup")


class DonchianStrategy(_LabBase):
    """Breakout: close menembus tertinggi/terendah 48 candle sebelumnya, searah EMA200."""
    VERSION = "lab_donchian/1.0.0"
    LOOK = 48

    def prepare(self, candles: Sequence[Candle]) -> None:
        super().prepare(candles)
        self._hh = _roll_max([c.high for c in candles], self.LOOK)
        self._ll = _roll_min([c.low for c in candles], self.LOOK)

    def signal_at(self, i: int) -> Signal:
        if i <= self.LOOK or self._hh[i - 1] is None or self._atr[i] is None or self._trend[i] is None:
            return self._no(i, "warmup")
        c, a, d = self._candles[i], self._atr[i], self._trend_dir(i)
        sd = self.cfg.atr_stop_mult * a
        rr = self.cfg.reward_risk
        if d > 0 and c.close > self._hh[i - 1]:
            return self._mk(i, Side.LONG, "donchian_up", c.close - sd, c.close + rr * sd)
        if d < 0 and c.close < self._ll[i - 1]:
            return self._mk(i, Side.SHORT, "donchian_down", c.close + sd, c.close - rr * sd)
        return self._no(i, "tidak_ada_setup")


class BollingerRevertStrategy(_LabBase):
    """Mean-reversion searah tren besar: close < BB(20,2) bawah & RSI<35 saat harga > EMA200 -> LONG ke tengah band."""
    VERSION = "lab_bbrev/1.0.0"
    N = 20
    K = 2.0
    MIN_RR = 1.0

    def prepare(self, candles: Sequence[Candle]) -> None:
        super().prepare(candles)
        closes = [c.close for c in candles]
        n = len(closes)
        self._mid: List[Optional[float]] = [None] * n
        self._sd: List[Optional[float]] = [None] * n
        for i in range(self.N - 1, n):
            w = closes[i - self.N + 1:i + 1]
            m = sum(w) / self.N
            self._mid[i] = m
            self._sd[i] = (sum((x - m) ** 2 for x in w) / self.N) ** 0.5

    def signal_at(self, i: int) -> Signal:
        if self._mid[i] is None or self._atr[i] is None or self._trend[i] is None or self._rsi[i] is None:
            return self._no(i, "warmup")
        c, a, r, d = self._candles[i], self._atr[i], self._rsi[i], self._trend_dir(i)
        m, sd = self._mid[i], self._sd[i]
        lo, up = m - self.K * sd, m + self.K * sd
        if d > 0 and c.close < lo and r < 35:
            return self._mk(i, Side.LONG, f"bb_revert_long rsi={r:.1f}", c.close - 1.5 * a, m)
        if d < 0 and c.close > up and r > 65:
            return self._mk(i, Side.SHORT, f"bb_revert_short rsi={r:.1f}", c.close + 1.5 * a, m)
        return self._no(i, "tidak_ada_setup")


class Rsi2Strategy(_LabBase):
    """Pantulan jangka pendek: RSI(2) < 10 saat harga > EMA200 -> LONG, TP 1.5 ATR, stop 1.5 ATR."""
    VERSION = "lab_rsi2/1.0.0"
    MIN_RR = 0.9

    def prepare(self, candles: Sequence[Candle]) -> None:
        super().prepare(candles)
        from .indicators import rsi as _rsi
        self._r2 = _rsi([c.close for c in candles], 2)

    def signal_at(self, i: int) -> Signal:
        if self._r2[i] is None or self._atr[i] is None or self._trend[i] is None:
            return self._no(i, "warmup")
        c, a, r, d = self._candles[i], self._atr[i], self._r2[i], self._trend_dir(i)
        if d > 0 and r < 10:
            return self._mk(i, Side.LONG, f"rsi2_long {r:.1f}", c.close - 1.5 * a, c.close + 1.5 * a)
        if d < 0 and r > 90:
            return self._mk(i, Side.SHORT, f"rsi2_short {r:.1f}", c.close + 1.5 * a, c.close - 1.5 * a)
        return self._no(i, "tidak_ada_setup")


class TrendHoldStrategy(_LabBase):
    """Trend-following 'tahan lama' untuk 4h/1d. LONG: close > EMA200, EMA50 > EMA200, close = tertinggi 20 candle.
    SHORT: kebalikannya (close < EMA200, EMA50 < EMA200, close = terendah 20 candle).
    Stop 2.5 ATR, TP sangat jauh (15 ATR); keluar utamanya lewat trailing stop (diatur di config)."""
    VERSION = "lab_trend/1.0.0"
    N = 20

    def prepare(self, candles: Sequence[Candle]) -> None:
        super().prepare(candles)
        self._hc = _roll_max([c.close for c in candles], self.N)
        self._lc = _roll_min([c.close for c in candles], self.N)

    def signal_at(self, i: int) -> Signal:
        if i < 1 or self._hc[i] is None or self._atr[i] is None or self._trend[i] is None or self._slow[i] is None:
            return self._no(i, "warmup")
        c, a = self._candles[i], self._atr[i]
        e50 = self._slow[i]
        if c.close > self._trend[i] and e50 > self._trend[i] and c.close >= self._hc[i]:
            return self._mk(i, Side.LONG, "trend_hold_up", c.close - 2.5 * a, c.close + 15 * a)
        if c.close < self._trend[i] and e50 < self._trend[i] and c.close <= self._lc[i]:
            return self._mk(i, Side.SHORT, "trend_hold_down", c.close + 2.5 * a, c.close - 15 * a)
        return self._no(i, "tidak_ada_setup")


LAB = {
    "lab_trend": TrendHoldStrategy,
    "lab_rsi2": Rsi2Strategy,
    "lab_pullback": PullbackStrategy,
    "lab_fib": FibStrategy,
    "lab_donchian": DonchianStrategy,
    "lab_bbrev": BollingerRevertStrategy,
}
