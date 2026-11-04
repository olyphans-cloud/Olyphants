"""Pabrik strategi berdasarkan cfg.strategy_mode (satu tempat, dipakai backtest/paper/portfolio)."""
from __future__ import annotations

from .config import Config
from .mtf_strategy import MultiTimeframeStrategy
from .strategies_lab import LAB
from .strategy_d1h4 import D1H4FuturesStrategy, D1H4SpotStrategy
from .strategy import EmaRsiTrendStrategy, H1TrendStrategy, M1ScalpStrategy


def make_strategy(cfg: Config):
    mode = cfg.strategy_mode
    if mode in LAB:
        return LAB[mode](cfg.strategy)
    if mode == "d1h4_spot":
        return D1H4SpotStrategy(cfg.strategy)
    if mode == "d1h4_futures":
        return D1H4FuturesStrategy(cfg.strategy)
    if mode == "mtf_1h_30m_15m":
        return MultiTimeframeStrategy(cfg.strategy)
    if mode == "h1_trend":
        return H1TrendStrategy(cfg.strategy)
    if mode == "m1_scalp":
        return M1ScalpStrategy(cfg.strategy)
    return EmaRsiTrendStrategy(cfg.strategy)
