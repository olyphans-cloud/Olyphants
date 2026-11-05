"""Backtest deterministik + metrik (gerbang G2)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .audit import AuditLog
from .config import Config, Mode, config_hash, validate
from .data import DataError, data_fingerprint, validate_candles
from .engine import Engine
from .execution import SimulatedExecutionAdapter
from .ledger import Ledger
from .models import Candle
from .risk import RiskEngine
from .strategies import make_strategy

DISCLAIMER = ("Hasil simulasi pada data yang diberikan; bukan bukti profitabilitas. "
              "Wajib validasi di luar sampel (G3), paper (G4), dan testnet (G5) sebelum apa pun.")


@dataclass
class BacktestResult:
    metrics: Dict[str, Any]
    trades: List[Dict[str, Any]]
    equity_curve: List[Tuple[int, float]]
    counters: Dict[str, int]
    data_fingerprint: str
    config_hash: str
    result_hash: str
    audit: AuditLog


def max_drawdown_pct(curve: Sequence[Tuple[int, float]]) -> float:
    peak, mdd = float("-inf"), 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak * 100.0)
    return mdd


def compute_metrics(initial: float, trades: Sequence[Dict[str, Any]],
                    curve: Sequence[Tuple[int, float]], fees: float) -> Dict[str, Any]:
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] <= 0]
    final = curve[-1][1] if curve else initial
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "bars": len(curve),
        "trades": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "net_pnl": final - initial,
        "return_pct": (final - initial) / initial * 100.0,
        "final_equity": final,
        "max_drawdown_pct": max_drawdown_pct(curve),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "total_fees": fees,
        "avg_trade": (sum(t["net_pnl"] for t in trades) / len(trades)) if trades else None,
        "disclaimer": DISCLAIMER,
    }


def run_backtest(candles: Sequence[Candle], cfg: Config, audit_path: Optional[str] = None) -> BacktestResult:
    cfg = validate(cfg)
    if cfg.mode != Mode.BACKTEST:
        raise ValueError("run_backtest hanya untuk mode BACKTEST")
    report = validate_candles(candles, cfg.interval_ms)  # tanpa now_ms: data historis
    if not report.ok:
        raise DataError("data tidak lolos validasi (G1): " + report.summary())

    chash = config_hash(cfg)
    audit = AuditLog(audit_path, chash)
    strategy = make_strategy(cfg)
    strategy.prepare(candles)
    risk = RiskEngine(cfg, Mode.BACKTEST)
    adapter = SimulatedExecutionAdapter(cfg.costs, Mode.BACKTEST, risk.verify)
    ledger = Ledger(cfg.initial_equity)
    engine = Engine(cfg, Mode.BACKTEST, strategy, risk, adapter, ledger, audit)

    fp = data_fingerprint(candles)
    audit.log("backtest_start", candles[0].ts, data_fingerprint=fp, bars=len(candles),
              strategy=strategy.VERSION)
    for i in range(len(candles)):
        engine.process_bar(candles, i)
    engine.finalize(candles)

    metrics = compute_metrics(cfg.initial_equity, ledger.trades, engine.equity_curve, ledger.fees_paid)
    blob = json.dumps({"trades": ledger.trades, "curve": engine.equity_curve, "m": {
        k: v for k, v in metrics.items() if k != "disclaimer"}}, sort_keys=True, default=str)
    rhash = hashlib.sha256(blob.encode()).hexdigest()
    audit.log("backtest_end", candles[-1].ts, result_hash=rhash, trades=len(ledger.trades),
              halted=risk.halted, halt_reason=risk.halt_reason)
    return BacktestResult(metrics, ledger.trades, engine.equity_curve, dict(engine.counters),
                          fp, chash, rhash, audit)
