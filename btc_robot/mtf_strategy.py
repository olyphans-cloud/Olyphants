"""Strategi multi-timeframe: 1H arah, 30M konfirmasi, 15M pemicu entry.

Input harus berupa candle 15M dengan timestamp epoch-ms UTC pada WAKTU BUKA.
Hanya candle 30M/1H yang benar-benar lengkap sampai penutupan candle 15M saat ini
boleh dipakai. Strategi hanya menghasilkan Signal; tidak dapat mengirim order.
"""
from __future__ import annotations
from bisect import bisect_right
from typing import List, Optional, Sequence
from .config import StrategyConfig
from .data import data_fingerprint
from .indicators import atr, ema, rsi
from .models import Candle, Side, Signal

M15 = 15 * 60_000
M30 = 30 * 60_000
H1 = 60 * 60_000


def _aggregate_complete(candles: Sequence[Candle], interval_ms: int) -> List[Candle]:
    """Aggregate only groups with all expected 15M candles; gaps are never filled."""
    groups = {}
    for c in candles:
        start = (c.ts // interval_ms) * interval_ms
        groups.setdefault(start, []).append(c)
    expected = interval_ms // M15
    out = []
    for start in sorted(groups):
        group = sorted(groups[start], key=lambda x: x.ts)
        if len(group) != expected or [x.ts for x in group] != [start + j * M15 for j in range(expected)]:
            continue
        out.append(Candle(start, group[0].open, max(x.high for x in group),
                          min(x.low for x in group), group[-1].close,
                          sum(x.volume for x in group)))
    return out


class MultiTimeframeStrategy:
    VERSION = "mtf_1h_30m_15m/1.0.0"

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self._candles: Sequence[Candle] = []
        self._fp = ""
        self._entry_ema: List[Optional[float]] = []
        self._entry_rsi: List[Optional[float]] = []
        self._entry_atr: List[Optional[float]] = []
        self._trend_bars: List[Candle] = []
        self._confirm_bars: List[Candle] = []
        self._trend_fast: List[Optional[float]] = []
        self._trend_slow: List[Optional[float]] = []
        self._confirm_fast: List[Optional[float]] = []
        self._confirm_slow: List[Optional[float]] = []
        self._trend_ts: List[int] = []
        self._confirm_ts: List[int] = []

    def prepare(self, candles: Sequence[Candle]) -> None:
        self._candles = candles
        closes = [c.close for c in candles]
        self._entry_ema = ema(closes, self.cfg.ema_fast)
        self._entry_rsi = rsi(closes, self.cfg.rsi_period)
        self._entry_atr = atr([c.high for c in candles], [c.low for c in candles], closes, self.cfg.atr_period)
        self._trend_bars = _aggregate_complete(candles, H1)
        self._confirm_bars = _aggregate_complete(candles, M30)
        self._trend_fast = ema([c.close for c in self._trend_bars], self.cfg.ema_fast)
        self._trend_slow = ema([c.close for c in self._trend_bars], self.cfg.ema_slow)
        self._confirm_fast = ema([c.close for c in self._confirm_bars], self.cfg.ema_fast)
        self._confirm_slow = ema([c.close for c in self._confirm_bars], self.cfg.ema_slow)
        self._trend_ts = [b.ts for b in self._trend_bars]
        self._confirm_ts = [b.ts for b in self._confirm_bars]
        self._fp = data_fingerprint(candles)

    @staticmethod
    def _latest_closed(bar_ts: Sequence[int], close_time: int, interval_ms: int) -> int:
        """Indeks bar terakhir yang sudah tutup pada close_time (bisect, O(log n))."""
        return bisect_right(bar_ts, close_time - interval_ms) - 1

    def _no(self, i: int, reason: str) -> Signal:
        c = self._candles[i]
        return Signal(c.ts, Side.NO_TRADE, reason, c.close, None, None, None, self.VERSION, self._fp)

    def signal_at(self, i: int) -> Signal:
        """Dipanggil pada candle 15M; semua TF lebih tinggi harus sudah tutup."""
        c = self._candles[i]
        if i < 1:
            return self._no(i, "warmup")
        close_time = c.ts + M15
        ti = self._latest_closed(self._trend_ts, close_time, H1)
        ci = self._latest_closed(self._confirm_ts, close_time, M30)
        if ti < 0 or ci < 0 or ti >= len(self._trend_bars) or ci >= len(self._confirm_bars):
            return self._no(i, "mtf_warmup_or_incomplete_candle")
        tf, ts = self._trend_fast[ti], self._trend_slow[ti]
        cf, cs = self._confirm_fast[ci], self._confirm_slow[ci]
        ef, rr, aa = self._entry_ema[i], self._entry_rsi[i], self._entry_atr[i]
        if any(v is None for v in (tf, ts, cf, cs, ef, rr, aa)):
            return self._no(i, "warmup")
        if aa <= 0:
            return self._no(i, "atr_nol")
        cfg = self.cfg
        stop_dist = cfg.atr_stop_mult * aa
        # 1H trend and 30M confirmation must agree; 15M close/RSI trigger the entry.
        if tf > ts and cf > cs and c.close > ef and cfg.rsi_long_min <= rr <= cfg.rsi_long_max:
            return Signal(c.ts, Side.LONG, f"1H_up_30M_confirm_15M_trigger rsi={rr:.1f}", c.close,
                          c.close - stop_dist, c.close + cfg.reward_risk * stop_dist,
                          None, self.VERSION, self._fp)
        if tf < ts and cf < cs and c.close < ef and cfg.rsi_short_min <= rr <= cfg.rsi_short_max:
            return Signal(c.ts, Side.SHORT, f"1H_down_30M_confirm_15M_trigger rsi={rr:.1f}", c.close,
                          c.close + stop_dist, c.close - cfg.reward_risk * stop_dist,
                          None, self.VERSION, self._fp)
        return self._no(i, "arah_1h_30m_tidak_sejalan_atau_15m_tidak_valid")
