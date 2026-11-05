"""Strategi D1 (filter tren) + H4 (trigger) - port dari bot_strategy.py milik pemilik ke arsitektur robot.

Input: candle 4H (epoch-ms UTC, waktu BUKA). Candle D1 DIBANGUN dari 6 candle 4H lengkap per hari UTC,
sehingga tidak perlu feed kedua. Candle D1 baru dipakai SETELAH tutup (tanpa lookahead).
Hanya menghasilkan Signal; ukuran posisi 1%, batas notional, cooldown, dan gerbang LIVE ada di RiskEngine.

  d1h4_spot    : long-only. D1 close > EMA200 & RSI(D1) > 50; H4 diskon (close < EMA50 atau RSI < 40) dan
                 EMA9 menyilang naik EMA21. Stop = swing low D1 (10 hari) x 0.98. Tanpa TP: keluar lewat
                 stop/trailing konfigurasi (menggantikan 'trailing D1 EMA21 / RSI>70' pada komentar asli).
  d1h4_futures : D1 close > EMA50 -> long saat H4 sweep low (low < low sebelumnya, close > low sebelumnya, RSI < 40);
                 D1 close < EMA50 -> short saat sweep high (RSI > 60). Stop 0.3% di luar sumbu; TP = reward_risk x risiko.
Perbedaan dari kode asli: RSI Wilder (standar); entry di harga open candle berikutnya (diisi engine);
'divergence' pada komentar asli tidak ada di kode asli dan tidak ditambahkan.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Dict, List, Optional, Sequence, Tuple

from .config import StrategyConfig
from .data import data_fingerprint
from .indicators import ema, rsi
from .models import Candle, Side, Signal

H4 = 4 * 3_600_000
D1 = 24 * 3_600_000
SWING_DAYS = 10
SWEEP_BUFFER = 0.003
SPOT_SL_MULT = 0.98

Setup = Optional[Tuple[Side, float, Optional[float], str]]   # (side, stop, take_profit, alasan)


def aggregate_d1(candles: Sequence[Candle]) -> List[Candle]:
    """Hanya hari UTC dengan 6 candle 4H lengkap dan berurutan; celah tidak pernah diisi."""
    groups: Dict[int, List[Candle]] = {}
    for c in candles:
        groups.setdefault((c.ts // D1) * D1, []).append(c)
    out: List[Candle] = []
    for start in sorted(groups):
        g = sorted(groups[start], key=lambda x: x.ts)
        if [x.ts for x in g] != [start + j * H4 for j in range(6)]:
            continue
        out.append(Candle(start, g[0].open, max(x.high for x in g), min(x.low for x in g),
                          g[-1].close, sum(x.volume for x in g)))
    return out


def spot_setup(v: Dict[str, float], rr: float) -> Setup:
    bullish = v["d1_close"] > v["d1_ema200"] and v["d1_rsi"] > 50
    discount = v["close"] < v["ema50"] or v["rsi"] < 40
    cross_up = v["ema9_prev"] <= v["ema21_prev"] and v["ema9"] > v["ema21"]
    if bullish and discount and cross_up:
        sl = v["d1_swing_low"] * SPOT_SL_MULT
        if sl < v["close"]:
            return Side.LONG, sl, None, "D1 bullish + H4 diskon + EMA9/21 cross"
    return None


def futures_setup(v: Dict[str, float], rr: float) -> Setup:
    c = v["close"]
    if v["d1_close"] > v["d1_ema50"] and v["low"] < v["prev_low"] and c > v["prev_low"] and v["rsi"] < 40:
        sl = v["low"] * (1 - SWEEP_BUFFER)
        risk = c - sl
        if risk > 0:
            return Side.LONG, sl, c + rr * risk, "D1 uptrend + H4 sweep low"
    if v["d1_close"] < v["d1_ema50"] and v["high"] > v["prev_high"] and c < v["prev_high"] and v["rsi"] > 60:
        sl = v["high"] * (1 + SWEEP_BUFFER)
        risk = sl - c
        if risk > 0:
            return Side.SHORT, sl, c - rr * risk, "D1 downtrend + H4 sweep high"
    return None


class _D1H4Base:
    VERSION = "d1h4/1.0.0"
    SETUP = staticmethod(spot_setup)

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self._candles: Sequence[Candle] = []
        self._fp = ""

    def prepare(self, candles: Sequence[Candle]) -> None:
        self._candles = candles
        closes = [c.close for c in candles]
        self._e9, self._e21, self._e50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
        self._rsi = rsi(closes, 14)
        d1 = aggregate_d1(candles)
        dc = [b.close for b in d1]
        self._d1_ts = [b.ts for b in d1]
        self._d1_close = dc
        self._d1_e50, self._d1_e200 = ema(dc, 50), ema(dc, 200)
        self._d1_rsi = rsi(dc, 14)
        lows = [b.low for b in d1]
        self._d1_swing: List[Optional[float]] = [
            min(lows[k - SWING_DAYS + 1:k + 1]) if k >= SWING_DAYS - 1 else None for k in range(len(lows))]
        self._fp = data_fingerprint(candles)

    def _no(self, i: int, reason: str) -> Signal:
        c = self._candles[i]
        return Signal(c.ts, Side.NO_TRADE, reason, c.close, None, None, None, self.VERSION, self._fp)

    def signal_at(self, i: int) -> Signal:
        if i < 1:
            return self._no(i, "warmup")
        c, p = self._candles[i], self._candles[i - 1]
        k = bisect_right(self._d1_ts, c.ts + H4 - D1) - 1       # D1 terakhir yang SUDAH tutup
        if k < 0:
            return self._no(i, "d1_belum_ada")
        v = {"close": c.close, "low": c.low, "high": c.high, "prev_low": p.low, "prev_high": p.high,
             "ema9": self._e9[i], "ema21": self._e21[i], "ema50": self._e50[i], "rsi": self._rsi[i],
             "ema9_prev": self._e9[i - 1], "ema21_prev": self._e21[i - 1],
             "d1_close": self._d1_close[k], "d1_ema50": self._d1_e50[k], "d1_ema200": self._d1_e200[k],
             "d1_rsi": self._d1_rsi[k], "d1_swing_low": self._d1_swing[k]}
        if any(x is None for x in v.values()):
            return self._no(i, "warmup")
        s = self.SETUP(v, self.cfg.reward_risk)
        if s is None:
            return self._no(i, "tidak_ada_setup")
        side, sl, tp, why = s
        return Signal(c.ts, side, why, c.close, sl, tp, None, self.VERSION, self._fp)


class D1H4SpotStrategy(_D1H4Base):
    VERSION = "d1h4_spot/1.0.0"
    SETUP = staticmethod(spot_setup)


class D1H4FuturesStrategy(_D1H4Base):
    VERSION = "d1h4_futures/1.0.0"
    SETUP = staticmethod(futures_setup)
