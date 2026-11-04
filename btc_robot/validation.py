"""Alat validasi out-of-sample (gerbang G3): walk-forward tanpa optimasi + holdout sekali-pakai."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .backtest import max_drawdown_pct, run_backtest
from .config import Config
from .models import Candle


class HoldoutReuseError(RuntimeError):
    pass


def split_holdout(candles: Sequence[Candle], holdout_fraction: float = 0.2) -> Tuple[List[Candle], List[Candle]]:
    if not (0.05 <= holdout_fraction <= 0.5):
        raise ValueError("holdout_fraction harus 0.05–0.5")
    cut = int(len(candles) * (1 - holdout_fraction))
    return list(candles[:cut]), list(candles[cut:])


def required_warmup(cfg: Config) -> int:
    """Jumlah candle minimum sebelum strategi bisa memberi sinyal pertama (+ margin)."""
    s = cfg.strategy
    if cfg.strategy_mode == "mtf_1h_30m_15m":
        # EMA lambat pada candle 1H butuh ema_slow bar 1H penuh = 4 candle 15m per bar,
        # ditambah hingga 1 jam penyelarasan batas jam.
        return (s.ema_slow + 1) * 4 + 4 + 8
    if cfg.strategy_mode.startswith("d1h4_"):
        return 200 * 6 + 6 + 60   # EMA200 D1 = 200 hari x 6 candle 4H, + hari parsial + margin
    if cfg.strategy_mode.startswith("lab_"):
        return 200 + 70  # EMA200 + jendela swing/Donchian
    return max(s.ema_slow, s.rsi_period, s.atr_period) + 10


def walk_forward(candles: Sequence[Candle], cfg: Config, n_windows: int = 4,
                 warmup_bars: Optional[int] = None) -> List[Dict[str, Any]]:
    """Menjalankan parameter TETAP pada jendela berurutan (tidak ada pemilihan parameter di sini).
    Setiap jendela diberi warm-up indikator dari data sebelumnya; metrik hanya dari jendela uji."""
    if warmup_bars is None:
        warmup_bars = max(120, required_warmup(cfg))
    n = len(candles)
    size = (n - warmup_bars) // n_windows
    if size < 50:
        raise ValueError("data terlalu pendek untuk jumlah jendela ini")
    out = []
    for w in range(n_windows):
        start = warmup_bars + w * size
        seg = list(candles[start - warmup_bars: start + size])
        res = run_backtest(seg, cfg)
        test_from = seg[warmup_bars].ts
        curve = [(ts, eq) for ts, eq in res.equity_curve if ts >= test_from]
        trades = [t for t in res.trades if t["entry_ts"] >= test_from]  # warm-up tidak dihitung
        start_eq = curve[0][1]
        end_eq = curve[-1][1]
        out.append({"window": w, "from_ts": test_from, "to_ts": seg[-1].ts,
                    "trades": len(trades),
                    "total_fees": sum(t["fees"] for t in trades),            # biaya komisi pada transaksi jendela uji
                    "net_pnl": sum(t["net_pnl"] for t in trades),            # setelah fee + spread + slippage
                    "win_rate": (sum(1 for t in trades if t["net_pnl"] > 0) / len(trades)) if trades else 0.0,
                    "return_pct": (end_eq - start_eq) / start_eq * 100.0,
                    "max_drawdown_pct": max_drawdown_pct(curve),
                    "result_hash": res.result_hash})
    return out


def evaluate_holdout_once(holdout: Sequence[Candle], cfg: Config, marker_path: str) -> Dict[str, Any]:
    """Holdout final hanya boleh dievaluasi SEKALI per marker; evaluasi kedua ditolak."""
    if os.path.exists(marker_path):
        raise HoldoutReuseError(f"holdout sudah pernah dievaluasi ({marker_path}); jangan dipakai untuk memilih parameter")
    res = run_backtest(holdout, cfg)
    os.makedirs(os.path.dirname(os.path.abspath(marker_path)), exist_ok=True)
    with open(marker_path, "w", encoding="utf-8") as fh:
        json.dump({"result_hash": res.result_hash, "config_hash": res.config_hash,
                   "data_fingerprint": res.data_fingerprint}, fh)
    return res.metrics
