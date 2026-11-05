"""Strategi contoh (EMA cross + filter RSI, stop berbasis ATR).

ATURAN ARSITEKTUR: modul ini TIDAK boleh meng-import risk/execution/ledger/engine.
Strategi hanya menghasilkan Signal; tidak punya jalur ke order. Ini kerangka uji, bukan klaim profit.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .config import StrategyConfig
from .data import data_fingerprint
from .indicators import atr, ema, rsi
from .models import Candle, Side, Signal


class EmaRsiTrendStrategy:
    VERSION = "ema_rsi_trend/1.0.0"

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self._candles: Sequence[Candle] = []
        self._fast: List[Optional[float]] = []
        self._slow: List[Optional[float]] = []
        self._rsi: List[Optional[float]] = []
        self._atr: List[Optional[float]] = []
        self._fp = ""

    def prepare(self, candles: Sequence[Candle]) -> None:
        self._candles = candles
        closes = [c.close for c in candles]
        self._fast = ema(closes, self.cfg.ema_fast)
        self._slow = ema(closes, self.cfg.ema_slow)
        self._rsi = rsi(closes, self.cfg.rsi_period)
        self._atr = atr([c.high for c in candles], [c.low for c in candles], closes, self.cfg.atr_period)
        self._fp = data_fingerprint(candles)

    def _no(self, i: int, reason: str) -> Signal:
        c = self._candles[i]
        return Signal(c.ts, Side.NO_TRADE, reason, c.close, None, None, None, self.VERSION, self._fp)

    def signal_at(self, i: int) -> Signal:
        """Sinyal pada PENUTUPAN candle i. Hanya memakai indeks <= i."""
        c = self._candles[i]
        if i < 1:
            return self._no(i, "warmup")
        vals = (self._fast[i], self._slow[i], self._fast[i - 1], self._slow[i - 1], self._rsi[i], self._atr[i])
        if any(v is None for v in vals):
            return self._no(i, "warmup")
        f, s, pf, ps, r, a = vals  # type: ignore[misc]
        if a <= 0:
            return self._no(i, "atr_nol")
        cfg = self.cfg
        stop_dist = cfg.atr_stop_mult * a
        if pf <= ps and f > s and c.close > s and cfg.rsi_long_min <= r <= cfg.rsi_long_max:
            return Signal(c.ts, Side.LONG, f"cross_up rsi={r:.1f}", c.close,
                          c.close - stop_dist, c.close + cfg.reward_risk * stop_dist,
                          None, self.VERSION, self._fp)
        if pf >= ps and f < s and c.close < s and cfg.rsi_short_min <= r <= cfg.rsi_short_max:
            return Signal(c.ts, Side.SHORT, f"cross_down rsi={r:.1f}", c.close,
                          c.close + stop_dist, c.close - cfg.reward_risk * stop_dist,
                          None, self.VERSION, self._fp)
        return self._no(i, "tidak_ada_setup")


class H1TrendStrategy(EmaRsiTrendStrategy):
    """Mode H1 (interval 1h): persilangan EMA + RSI, hanya jika EMA lambat searah selama 3 candle.

    Mandiri dari mode M1 dan MTF: parameter, versi, dan filter sendiri.
    """
    VERSION = "h1_trend/1.0.0"
    SLOPE_BARS = 3

    def signal_at(self, i: int) -> Signal:
        base = super().signal_at(i)
        if base.side == Side.NO_TRADE:
            return base
        c = self._candles[i]
        if i < self.SLOPE_BARS or self._slow[i - self.SLOPE_BARS] is None:
            return self._no(i, "warmup")
        a = self._atr[i]
        if self.cfg.min_atr_pct > 0 and a is not None and a / c.close * 100.0 < self.cfg.min_atr_pct:
            return self._no(i, "atr_pct_rendah")
        slope = self._slow[i] - self._slow[i - self.SLOPE_BARS]  # type: ignore[operator]
        if base.side == Side.LONG and slope <= 0:
            return self._no(i, "h1_ema_lambat_tidak_naik")
        if base.side == Side.SHORT and slope >= 0:
            return self._no(i, "h1_ema_lambat_tidak_turun")
        return base


class M1ScalpStrategy(EmaRsiTrendStrategy):
    """Mode M1 (interval 1m): lanjutan tren setelah pullback ke EMA cepat.

    LONG  : EMA cepat > lambat, close sebelumnya <= EMA cepat, close sekarang > EMA cepat, RSI dalam rentang.
    SHORT : kebalikannya. Filter ATR% minimum agar gerak cukup besar dibanding biaya; kelayakan
    terhadap fee/spread dinilai oleh RiskEngine (min_edge_cost_mult), bukan oleh strategi.
    """
    VERSION = "m1_scalp/1.0.0"

    def signal_at(self, i: int) -> Signal:
        c = self._candles[i]
        if i < 1:
            return self._no(i, "warmup")
        vals = (self._fast[i], self._slow[i], self._fast[i - 1], self._rsi[i], self._atr[i])
        if any(v is None for v in vals):
            return self._no(i, "warmup")
        f, s, pf, r, a = vals  # type: ignore[misc]
        if a <= 0:
            return self._no(i, "atr_nol")
        cfg = self.cfg
        if cfg.min_atr_pct > 0 and a / c.close * 100.0 < cfg.min_atr_pct:
            return self._no(i, "atr_pct_rendah")
        prev_close = self._candles[i - 1].close
        stop_dist = cfg.atr_stop_mult * a
        if f > s and prev_close <= pf and c.close > f and cfg.rsi_long_min <= r <= cfg.rsi_long_max:
            return Signal(c.ts, Side.LONG, f"m1_pullback_up rsi={r:.1f}", c.close,
                          c.close - stop_dist, c.close + cfg.reward_risk * stop_dist,
                          None, self.VERSION, self._fp)
        if f < s and prev_close >= pf and c.close < f and cfg.rsi_short_min <= r <= cfg.rsi_short_max:
            return Signal(c.ts, Side.SHORT, f"m1_pullback_down rsi={r:.1f}", c.close,
                          c.close + stop_dist, c.close - cfg.reward_risk * stop_dist,
                          None, self.VERSION, self._fp)
        return self._no(i, "tidak_ada_setup")
