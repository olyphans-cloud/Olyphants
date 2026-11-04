"""Konfigurasi tervalidasi. LIVE/TESTNET terkunci (Dok 01 bagian 5, 9, 10)."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Mapping, Optional

CODE_VERSION = "0.3.0"
LIVE_CONFIRM_PHRASE = "I-ACCEPT-REAL-MONEY-RISK"

INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


class Mode(str, Enum):
    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class ConfigError(ValueError):
    pass


STRATEGY_MODES = frozenset({"ema_rsi", "mtf_1h_30m_15m", "h1_trend", "m1_scalp",
                            "lab_pullback", "lab_fib", "lab_donchian", "lab_bbrev", "lab_rsi2", "lab_trend",
                            "d1h4_spot", "d1h4_futures"})


@dataclass(frozen=True)
class CostConfig:
    fee_rate: float = 0.001          # per sisi, fraksi (0.001 = 0.1%)
    spread_bps: float = 2.0          # spread penuh; setengahnya dibayar per sisi
    slippage_bps: float = 3.0        # per sisi
    lot_step: float = 0.00001
    min_qty: float = 0.00001
    min_notional: float = 5.0


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_notional_pct_of_equity: float = 100.0   # tanpa leverage
    max_daily_loss_pct: float = 2.0
    max_drawdown_pct: float = 10.0              # latch halt
    max_open_positions: int = 1
    min_stop_distance_pct: float = 0.2
    max_stop_distance_pct: float = 5.0
    max_data_age_seconds: int = 180
    max_signal_age_bars: int = 1
    allow_short: bool = False
    max_loss_streak: int = 3                    # loss beruntun sebelum cooldown
    loss_streak_cooldown_sec: int = 14_400      # 4 jam (harus >> interval candle)
    max_entries_per_hour: int = 1               # batas entry per jam UTC (mode M1 butuh lebih)
    max_hold_sec: int = 0                       # 0 = nonaktif; >0 = tutup paksa di close candle
    trailing_trail_pct: float = 0.0             # 0 = nonaktif
    trailing_activation_pct: float = 1.0        # laba minimum (%) sebelum trailing aktif
    require_spread_check: bool = False          # PAPER: tolak entry tanpa kuotasi order book sehat
    max_spread_pct: float = 0.15
    max_orderbook_age_sec: float = 5.0
    min_edge_cost_mult: float = 0.0             # 0 = nonaktif; TP% harus >= mult x biaya pulang-pergi%


@dataclass(frozen=True)
class StrategyConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    reward_risk: float = 2.0
    rsi_long_min: float = 50.0
    rsi_long_max: float = 75.0
    rsi_short_min: float = 25.0
    rsi_short_max: float = 50.0
    min_atr_pct: float = 0.0                    # 0 = nonaktif; ATR/harga minimum (%)


@dataclass(frozen=True)
class Config:
    mode: Mode = Mode.BACKTEST
    symbol: str = "BTCUSDT"
    interval: str = "1h"
    strategy_mode: str = "ema_rsi"  # ema_rsi | mtf_1h_30m_15m | h1_trend | m1_scalp
    initial_equity: float = 1000.0
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    state_dir: str = "state"
    shared_kill_switch_file: str = ""       # opsional: satu berkas yang menghentikan SEMUA mode
    gates_file: str = "state/release_gates.json"

    @property
    def interval_ms(self) -> int:
        return INTERVAL_MS[self.interval]

    @property
    def kill_switch_file(self) -> str:
        return os.path.join(self.state_dir, "KILL_SWITCH")

    @property
    def sleeve_id(self) -> str:
        return f"{self.strategy_mode}|{self.symbol}|{self.interval}"


def _plain(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return {f.name: _plain(getattr(obj, f.name)) for f in fields(obj)}
    return obj


def config_hash(cfg: Config) -> str:
    blob = json.dumps(_plain(cfg), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _build(cls, data: Mapping[str, Any], path: str):
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"kunci tidak dikenal di {path}: {sorted(unknown)}")
    kwargs = {}
    for k, v in data.items():
        if k == "mode":
            try:
                v = Mode(v)
            except ValueError:
                raise ConfigError(f"mode tidak valid: {v!r}")
        elif k == "costs":
            v = _build(CostConfig, v, "costs")
        elif k == "risk":
            v = _build(RiskConfig, v, "risk")
        elif k == "strategy":
            v = _build(StrategyConfig, v, "strategy")
        kwargs[k] = v
    return cls(**kwargs)


def config_from_dict(data: Mapping[str, Any]) -> Config:
    return _build(Config, data, "root")


def load_config(path: Optional[str]) -> Config:
    if not path:
        return Config()
    with open(path, "r", encoding="utf-8") as fh:
        return config_from_dict(json.load(fh))


def validate(cfg: Config, env: Optional[Mapping[str, str]] = None) -> Config:
    """Validasi rentang + kunci mode. Mengembalikan cfg atau melempar ConfigError."""
    env = os.environ if env is None else env
    if cfg.interval not in INTERVAL_MS:
        raise ConfigError(f"interval tidak didukung: {cfg.interval}")
    if cfg.initial_equity <= 0:
        raise ConfigError("initial_equity harus > 0")
    c, r, s = cfg.costs, cfg.risk, cfg.strategy
    if cfg.strategy_mode not in STRATEGY_MODES:
        raise ConfigError(f"strategy_mode harus salah satu dari {sorted(STRATEGY_MODES)}")
    if cfg.strategy_mode == "h1_trend" and cfg.interval != "1h":
        raise ConfigError("strategy_mode h1_trend mewajibkan interval=1h")
    if cfg.strategy_mode == "m1_scalp" and cfg.interval != "1m":
        raise ConfigError("strategy_mode m1_scalp mewajibkan interval=1m")
    if cfg.strategy_mode == "mtf_1h_30m_15m" and cfg.interval != "15m":
        raise ConfigError("strategy_mode mtf_1h_30m_15m mewajibkan interval=15m")
    if cfg.strategy_mode in ("d1h4_spot", "d1h4_futures") and cfg.interval != "4h":
        raise ConfigError(f"strategy_mode {cfg.strategy_mode} mewajibkan interval=4h (D1 dibangun dari candle 4H)")
    if cfg.strategy_mode == "d1h4_spot" and r.allow_short:
        raise ConfigError("d1h4_spot adalah long-only: risk.allow_short harus false")
    if not (0 <= c.fee_rate < 0.05):
        raise ConfigError("fee_rate di luar rentang wajar [0, 0.05)")
    if c.spread_bps < 0 or c.slippage_bps < 0:
        raise ConfigError("spread/slippage tidak boleh negatif")
    if c.lot_step <= 0 or c.min_qty <= 0 or c.min_notional < 0:
        raise ConfigError("lot_step/min_qty/min_notional tidak valid")
    if not (0 < r.risk_per_trade_pct <= 2.0):
        raise ConfigError("risk_per_trade_pct harus di (0, 2]; naikkan hanya dengan persetujuan pemilik")
    if not (0 < r.max_notional_pct_of_equity <= 100.0):
        raise ConfigError("max_notional_pct_of_equity harus di (0, 100] (tanpa leverage)")
    if not (0 < r.max_daily_loss_pct <= 10):
        raise ConfigError("max_daily_loss_pct harus di (0, 10]")
    if not (0 < r.max_drawdown_pct <= 50):
        raise ConfigError("max_drawdown_pct harus di (0, 50]")
    if r.max_open_positions != 1:
        raise ConfigError("saat ini hanya max_open_positions=1 yang didukung")
    if not (0 < r.min_stop_distance_pct < r.max_stop_distance_pct <= 20):
        raise ConfigError("rentang jarak stop tidak valid")
    if r.max_loss_streak < 1:
        raise ConfigError("max_loss_streak harus >= 1")
    if not (cfg.interval_ms // 1000 <= r.loss_streak_cooldown_sec <= 7 * 86_400):
        raise ConfigError("loss_streak_cooldown_sec harus antara 1 candle dan 7 hari "
                          "(cooldown lebih pendek dari satu candle tidak berfungsi)")
    if not (1 <= r.max_entries_per_hour <= 60):
        raise ConfigError("max_entries_per_hour harus di [1, 60]")
    if r.max_hold_sec < 0 or (r.max_hold_sec and not (cfg.interval_ms // 1000 <= r.max_hold_sec <= 30 * 86_400)):
        raise ConfigError("max_hold_sec harus 0 (nonaktif) atau antara 1 candle dan 30 hari")
    if not (0 <= r.trailing_trail_pct < 20) or not (0 <= r.trailing_activation_pct <= 50):
        raise ConfigError("trailing_trail_pct harus di [0, 20) dan trailing_activation_pct di [0, 50]")
    if not (0 < r.max_spread_pct <= 5) or r.max_orderbook_age_sec <= 0:
        raise ConfigError("max_spread_pct harus di (0, 5] dan max_orderbook_age_sec > 0")
    if r.min_edge_cost_mult < 0:
        raise ConfigError("min_edge_cost_mult tidak boleh negatif")
    if r.max_data_age_seconds <= 0 or r.max_signal_age_bars < 1:
        raise ConfigError("batas umur data/sinyal tidak valid")
    if not (2 <= s.ema_fast < s.ema_slow):
        raise ConfigError("butuh 2 <= ema_fast < ema_slow")
    if s.min_atr_pct < 0:
        raise ConfigError("min_atr_pct tidak boleh negatif")
    if s.rsi_period < 2 or s.atr_period < 2 or s.atr_stop_mult <= 0 or s.reward_risk <= 0:
        raise ConfigError("parameter strategi tidak valid")

    if cfg.mode == Mode.TESTNET:
        raise ConfigError("TESTNET belum diimplementasikan (gerbang G6 belum terpenuhi)")
    if cfg.mode == Mode.LIVE:
        from . import gates  # impor lokal: hindari siklus
        gates.require_live_unlock(cfg, env)
        raise ConfigError("adapter LIVE sengaja belum diimplementasikan di versi ini")
    return cfg


def roundtrip_cost_pct(cfg: Config) -> float:
    """Biaya pulang-pergi (% dari harga): 2 x (fee + setengah spread + slippage). Sama dengan rumus RiskEngine."""
    c = cfg.costs
    return 2.0 * (c.fee_rate * 100.0 + (c.spread_bps / 2.0 + c.slippage_bps) / 100.0)


def config_warnings(cfg: Config) -> list:
    """Peringatan statis (bukan error, tidak mengubah perilaku): kombinasi parameter yang secara aritmetika
    membuat biaya menggerus edge. Dicetak ke stderr oleh CLI."""
    out = []
    rt = roundtrip_cost_pct(cfg)
    r = cfg.risk
    if r.min_stop_distance_pct < rt:
        out.append(f"min_stop_distance_pct ({r.min_stop_distance_pct}%) lebih kecil dari biaya pulang-pergi ({rt:.3f}%): "
                   "stop bisa lebih sempit dari biaya; kerugian per stop >= biaya + jarak stop")
    if r.trailing_trail_pct > 0 and (r.trailing_activation_pct - r.trailing_trail_pct) < rt:
        out.append(f"trailing mengunci profit awal {r.trailing_activation_pct - r.trailing_trail_pct:.3f}% "
                   f"(aktivasi - jarak), di bawah biaya pulang-pergi {rt:.3f}%: keluar lewat trailing pertama bisa net rugi")
    return out
