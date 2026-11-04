"""Scanner multi-koin + gerbang risiko bersama + simulasi beberapa mode/koin sekaligus.

Satu "sleeve" = satu pasangan (simbol, mode strategi) dengan ledger/RiskEngine/Engine sendiri.
PortfolioRiskGate menambah batas LINTAS-sleeve di atas risiko per-sleeve (keduanya harus lolos):
  - jumlah posisi terbuka total, posisi per simbol (mencegah H1 dan M15 menumpuk di simbol yang sama),
  - eksposur total (% ekuitas total), rugi harian total (blok sampai hari UTC berikutnya),
  - drawdown total (latch HALT sampai ditinjau manusia).
Gerbang ini hanya bisa MENOLAK entry; tidak pernah membuat atau memperbesar order.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .audit import AuditLog
from .backtest import compute_metrics, max_drawdown_pct
from .config import Config, Mode, config_hash, validate
from .data import DataError, validate_candles
from .engine import Engine
from .execution import SimulatedExecutionAdapter
from .indicators import atr
from .ledger import Ledger
from .models import ApprovedOrder, BookQuote, Candle
from .risk import RiskEngine
from .strategies import make_strategy

DAY_MS = 86_400_000


# ----------------------------------------------------------------------------------------------
# Scanner
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ScanConfig:
    mode: str = "auto"                  # "manual" = whitelist + filter keselamatan; "auto" = ranking top_n
    symbols: Tuple[str, ...] = ()       # wajib untuk manual
    top_n: int = 3                      # hanya untuk auto
    lookback_bars: int = 96
    min_avg_quote_volume: float = 0.0   # rata-rata (volume x close) per candle
    min_atr_pct: float = 0.0
    max_atr_pct: float = 0.0            # 0 = tanpa batas atas
    max_spread_pct: float = 0.0         # 0 = tanpa filter; hanya jika quotes diberikan
    max_data_age_bars: int = 2


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    selected: bool
    score: float
    reasons: Tuple[str, ...]
    metrics: Mapping[str, float] = field(default_factory=dict)


def _interval_of(candles: Sequence[Candle]) -> Optional[int]:
    return candles[-1].ts - candles[-2].ts if len(candles) >= 2 else None


def scan(data: Mapping[str, Sequence[Candle]], cfg: ScanConfig, now_ms: Optional[int] = None,
         quotes: Optional[Mapping[str, BookQuote]] = None) -> List[ScanResult]:
    """Deterministik. Filter keselamatan berlaku di mode manual maupun auto."""
    if cfg.mode not in ("manual", "auto"):
        raise ValueError("ScanConfig.mode harus manual atau auto")
    if cfg.mode == "manual" and not cfg.symbols:
        raise ValueError("mode manual wajib mengisi symbols (whitelist)")
    if cfg.mode == "auto" and cfg.top_n < 1:
        raise ValueError("top_n harus >= 1")
    candidates = list(cfg.symbols) if cfg.mode == "manual" else sorted(data)
    if now_ms is None:
        ends = [c[-1].ts + (_interval_of(c) or 0) for c in data.values() if len(c) >= 2]
        now_ms = max(ends) if ends else 0

    eligible: List[Tuple[str, Dict[str, float]]] = []
    rejected: Dict[str, Tuple[List[str], Dict[str, float]]] = {}
    for sym in candidates:
        candles = data.get(sym)
        if not candles:
            rejected[sym] = (["data_tidak_ada"], {})
            continue
        itv = _interval_of(candles)
        need = cfg.lookback_bars + 15
        if itv is None or itv <= 0 or len(candles) < need:
            rejected[sym] = (["data_kurang"], {})
            continue
        why: List[str] = []
        age_bars = (now_ms - (candles[-1].ts + itv)) / itv
        window = candles[-cfg.lookback_bars:]
        avg_qv = sum(c.volume * c.close for c in window) / len(window)
        a = atr([c.high for c in candles], [c.low for c in candles], [c.close for c in candles], 14)[-1]
        atr_pct = (a / candles[-1].close * 100.0) if a else 0.0
        m: Dict[str, float] = {"avg_quote_volume": avg_qv, "atr_pct": atr_pct, "data_age_bars": age_bars}
        if age_bars > cfg.max_data_age_bars:
            why.append("data_basi")
        if a is None or atr_pct <= 0:
            why.append("atr_tidak_valid")
        if avg_qv < cfg.min_avg_quote_volume:
            why.append("likuiditas_rendah")
        if atr_pct < cfg.min_atr_pct:
            why.append("volatilitas_rendah")
        if cfg.max_atr_pct > 0 and atr_pct > cfg.max_atr_pct:
            why.append("volatilitas_terlalu_tinggi")
        if cfg.max_spread_pct > 0 and quotes is not None:
            q = quotes.get(sym)
            if q is None or q.bid <= 0 or q.ask < q.bid:
                why.append("spread_tidak_diketahui")
            else:
                sp = (q.ask - q.bid) / q.bid * 100.0
                m["spread_pct"] = sp
                if sp > cfg.max_spread_pct:
                    why.append("spread_lebar")
        if why:
            rejected[sym] = (why, m)
        else:
            eligible.append((sym, m))

    max_qv = max((m["avg_quote_volume"] for _, m in eligible), default=0.0) or 1.0
    max_atr = max((m["atr_pct"] for _, m in eligible), default=0.0) or 1.0
    scored = sorted(((0.7 * m["avg_quote_volume"] / max_qv + 0.3 * m["atr_pct"] / max_atr, sym, m)
                     for sym, m in eligible), key=lambda t: (-t[0], t[1]))
    chosen = {sym for _, sym, _ in (scored if cfg.mode == "manual" else scored[:cfg.top_n])}
    out = [ScanResult(sym, sym in chosen, round(sc, 6), (), m) for sc, sym, m in scored]
    out += [ScanResult(sym, False, 0.0, tuple(w), m) for sym, (w, m) in sorted(rejected.items())]
    return sorted(out, key=lambda r: (not r.selected, -r.score, r.symbol))


# ----------------------------------------------------------------------------------------------
# Gerbang risiko bersama
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PortfolioLimits:
    max_open_positions: int = 2
    max_positions_per_symbol: int = 1
    max_total_exposure_pct: float = 100.0
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 15.0

    def __post_init__(self) -> None:
        if self.max_open_positions < 1 or self.max_positions_per_symbol < 1:
            raise ValueError("batas posisi harus >= 1")
        if not (0 < self.max_total_exposure_pct <= 100):
            raise ValueError("max_total_exposure_pct harus di (0, 100] (tanpa leverage)")
        if not (0 < self.max_daily_loss_pct <= 20) or not (0 < self.max_drawdown_pct <= 50):
            raise ValueError("batas rugi harian/drawdown portofolio di luar rentang")


class PortfolioRiskGate:
    def __init__(self, limits: PortfolioLimits):
        self.limits = limits
        self._sleeves: Dict[str, Tuple[str, Ledger]] = {}
        self.marks: Dict[str, float] = {}
        self.peak = 0.0
        self.day_key: Optional[int] = None
        self.day_start = 0.0
        self.day_blocked = False
        self.halted = False
        self.halt_reason: Optional[str] = None
        self.counters: Counter = Counter()

    def register(self, name: str, symbol: str, ledger: Ledger) -> None:
        if name in self._sleeves:
            raise ValueError(f"nama sleeve ganda: {name}")
        self._sleeves[name] = (symbol, ledger)
        self.peak += ledger.initial_equity

    def set_mark(self, symbol: str, price: float) -> None:
        self.marks[symbol] = price

    def total_equity(self) -> float:
        return sum(l.equity(self.marks.get(sym, 0.0)) for sym, l in self._sleeves.values())

    def exposure(self) -> float:
        return sum(abs(l.position.qty) * self.marks.get(sym, 0.0)
                   for sym, l in self._sleeves.values() if l.position is not None)

    def update(self, ts_ms: int) -> None:
        eq = self.total_equity()
        key = ts_ms // DAY_MS
        if key != self.day_key:
            self.day_key, self.day_start, self.day_blocked = key, eq, False
        self.peak = max(self.peak, eq)
        if self.day_start > 0 and (self.day_start - eq) / self.day_start * 100.0 >= self.limits.max_daily_loss_pct:
            self.day_blocked = True
        if not self.halted and self.peak > 0:
            dd = (self.peak - eq) / self.peak * 100.0
            if dd >= self.limits.max_drawdown_pct:
                self.halted, self.halt_reason = True, f"portfolio_drawdown {dd:.2f}%"

    def check(self, sleeve: str, order: ApprovedOrder, bar: Candle) -> Tuple[bool, str]:
        """(diizinkan, alasan). Alasan dipakai sebagai kunci penghitung penolakan."""
        ok, why = self._check(sleeve, order)
        self.counters["ok" if ok else why] += 1
        return ok, why

    def _check(self, sleeve: str, order: ApprovedOrder) -> Tuple[bool, str]:
        if self.halted:
            return False, "portfolio_halt"
        if self.day_blocked:
            return False, "portfolio_daily_loss"
        symbol, _ = self._sleeves[sleeve]  # KeyError => engine menolak (gate_error)
        open_now = [(s, sym) for s, (sym, l) in self._sleeves.items() if l.position is not None]
        if len(open_now) >= self.limits.max_open_positions:
            return False, "max_open_positions"
        if sum(1 for _, sym in open_now if sym == symbol) >= self.limits.max_positions_per_symbol:
            return False, "symbol_position_limit"
        notional = order.qty * order.ref_price
        if self.exposure() + notional > self.total_equity() * self.limits.max_total_exposure_pct / 100.0:
            return False, "max_total_exposure"
        return True, "ok"


# ----------------------------------------------------------------------------------------------
# Simulasi multi-sleeve (backtest, kausal)
# ----------------------------------------------------------------------------------------------
@dataclass
class SleeveSpec:
    name: str
    cfg: Config
    candles: Sequence[Candle]


@dataclass
class PortfolioResult:
    metrics: Dict[str, Any]
    sleeves: Dict[str, Dict[str, Any]]
    gate_counters: Dict[str, int]
    scan: List[ScanResult]
    skipped: List[str]
    result_hash: str
    trades: List[Dict[str, Any]] = field(default_factory=list)  # semua trade, ditandai 'sleeve' dan 'interval_ms'


def run_portfolio_backtest(specs: Sequence[SleeveSpec], limits: Optional[PortfolioLimits] = None,
                           scan_cfg: Optional[ScanConfig] = None) -> PortfolioResult:
    """Event kausal: begin_bar diurut menurut waktu BUKA, end_bar menurut waktu TUTUP (end sebelum begin
    pada waktu sama), sehingga sleeve berinterval berbeda (H1 + M15) saling melihat keadaan yang sah."""
    if not specs:
        raise ValueError("minimal satu sleeve")
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ValueError("nama sleeve harus unik")
    limits = limits or PortfolioLimits()

    scan_results: List[ScanResult] = []
    skipped: List[str] = []
    active = list(specs)
    if scan_cfg is not None:
        data = {s.cfg.symbol: s.candles for s in specs}
        scan_results = scan(data, scan_cfg)
        selected = {r.symbol for r in scan_results if r.selected}
        active = [s for s in specs if s.cfg.symbol in selected]
        skipped = [s.name for s in specs if s.cfg.symbol not in selected]
    if not active:
        raise ValueError("scanner tidak memilih satu pun simbol; tidak ada yang disimulasikan")

    gate = PortfolioRiskGate(limits)
    ctx: List[Dict[str, Any]] = []
    for s in active:
        cfg = validate(s.cfg)
        if cfg.mode != Mode.BACKTEST:
            raise ValueError("run_portfolio_backtest hanya untuk mode BACKTEST")
        report = validate_candles(s.candles, cfg.interval_ms)
        if not report.ok:
            raise DataError(f"data sleeve {s.name} tidak lolos validasi: {report.summary()}")
        strategy = make_strategy(cfg)
        strategy.prepare(s.candles)
        risk = RiskEngine(cfg, Mode.BACKTEST)
        adapter = SimulatedExecutionAdapter(cfg.costs, Mode.BACKTEST, risk.verify)
        ledger = Ledger(cfg.initial_equity)
        audit = AuditLog(None, config_hash(cfg))
        engine = Engine(cfg, Mode.BACKTEST, strategy, risk, adapter, ledger, audit,
                        entry_gate=(lambda order, bar, n=s.name: gate.check(n, order, bar)))
        gate.register(s.name, cfg.symbol, ledger)
        ctx.append({"spec": s, "cfg": cfg, "engine": engine, "ledger": ledger, "risk": risk})

    events: List[Tuple[int, int, int, int]] = []  # (waktu, fase 0=end/1=begin, indeks sleeve, indeks candle)
    for si, c in enumerate(ctx):
        itv = c["cfg"].interval_ms
        for i, bar in enumerate(c["spec"].candles):
            events.append((bar.ts + itv, 0, si, i))
            events.append((bar.ts, 1, si, i))
    events.sort()

    curve: List[Tuple[int, float]] = []
    for t, phase, si, i in events:
        c = ctx[si]
        candles = c["spec"].candles
        bar = candles[i]
        sym = c["cfg"].symbol
        if phase == 1:
            gate.set_mark(sym, bar.open)
            gate.update(t)
            c["engine"].begin_bar(bar)
        else:
            gate.set_mark(sym, bar.close)
            c["engine"].end_bar(candles, i)
            gate.update(t)
            curve.append((t, gate.total_equity()))
    for c in ctx:
        c["engine"].finalize(c["spec"].candles)
    last_t = curve[-1][0] if curve else 0
    for c in ctx:
        gate.set_mark(c["cfg"].symbol, c["spec"].candles[-1].close)
    curve.append((last_t, gate.total_equity()))

    initial = sum(c["cfg"].initial_equity for c in ctx)
    final = gate.total_equity()
    sleeves: Dict[str, Dict[str, Any]] = {}
    all_trades: List[Dict[str, Any]] = []
    for c in ctx:
        led, eng = c["ledger"], c["engine"]
        sleeves[c["spec"].name] = {
            "symbol": c["cfg"].symbol, "strategy_mode": c["cfg"].strategy_mode,
            "metrics": compute_metrics(c["cfg"].initial_equity, led.trades, eng.equity_curve, led.fees_paid),
            "counters": dict(eng.counters), "halted": c["risk"].halted, "halt_reason": c["risk"].halt_reason,
        }
        all_trades += [dict(t, sleeve=c["spec"].name, symbol=c["cfg"].symbol, interval_ms=c["cfg"].interval_ms)
                       for t in led.trades]
    metrics = {
        "initial_equity": initial, "final_equity": final,
        "return_pct": (final - initial) / initial * 100.0,
        "max_drawdown_pct": max_drawdown_pct(curve),
        "trades": len(all_trades),
        "portfolio_halted": gate.halted, "portfolio_halt_reason": gate.halt_reason,
    }
    blob = json.dumps({"trades": all_trades, "curve": curve, "m": metrics}, sort_keys=True, default=str)
    return PortfolioResult(metrics, sleeves, dict(gate.counters), scan_results, skipped,
                           hashlib.sha256(blob.encode()).hexdigest(), all_trades)


# ----------------------------------------------------------------------------------------------
# Pemuat spesifikasi (JSON) untuk CLI
# ----------------------------------------------------------------------------------------------
_SYNTH_VOL = {"1m": 0.0007}  # interval lain memakai default generator (0.008) agar hasil lama tidak bergeser


def synth_vol(interval: str) -> float:
    return _SYNTH_VOL.get(interval, 0.008)


def load_portfolio_spec(path: str) -> Tuple[List[SleeveSpec], PortfolioLimits, Optional[ScanConfig]]:
    """Spesifikasi JSON: {"limits":{..}, "scan":{..}|null, "sleeves":[{"name","config","csv"|"synthetic":{..}}]}.

    Path relatif dihitung dari folder berkas spesifikasi. Data "synthetic" HANYA untuk uji mesin.
    """
    import os
    from .config import load_config
    from .data import generate_synthetic, load_csv

    base = os.path.dirname(os.path.abspath(path))
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    def rel(p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(base, p)

    specs: List[SleeveSpec] = []
    for sl in raw.get("sleeves", []):
        cfg = load_config(rel(sl["config"]))
        if "symbol" in sl:
            from dataclasses import replace as _replace
            cfg = _replace(cfg, symbol=sl["symbol"])
        if "csv" in sl:
            candles = load_csv(rel(sl["csv"]))
        elif "synthetic" in sl:
            sy = sl["synthetic"]
            candles = generate_synthetic(int(sy["n"]), int(sy.get("seed", 1)), cfg.interval_ms,
                                         start_ts=int(sy.get("start_ts", 1_700_000_000_000 // 3_600_000 * 3_600_000)),
                                         start_price=float(sy.get("start_price", 30_000.0)),
                                         vol=float(sy.get("vol", synth_vol(cfg.interval))))
        else:
            raise ValueError(f"sleeve {sl.get('name')}: butuh 'csv' atau 'synthetic'")
        specs.append(SleeveSpec(sl["name"], cfg, candles))
    limits = PortfolioLimits(**raw.get("limits", {}))
    sc = raw.get("scan")
    scan_cfg = None
    if sc:
        sc = dict(sc)
        sc["symbols"] = tuple(sc.get("symbols", ()))
        scan_cfg = ScanConfig(**sc)
    return specs, limits, scan_cfg
