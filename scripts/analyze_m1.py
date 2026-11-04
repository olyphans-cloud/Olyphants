#!/usr/bin/env python3
"""Analisis M1/H1: rincian transaksi, atribusi rugi, pembanding entry-acak, regime, ablasi, walk-forward/holdout.

PENTING
- Semua angka dari data SINTETIS kecuali --csv diberikan. Data sintetis adalah random walk (tanpa edge by
  construction) sehingga hasilnya TIDAK membuktikan apa pun tentang pasar nyata.
- Ablasi/sensitivitas adalah DIAGNOSTIK. Tidak ada parameter yang dipilih/diubah dari hasil ini.
- Skrip tidak mengubah asumsi biaya agar hasil tampak lebih baik; semua variasi dilaporkan penuh.

Pakai (dari folder olypahantstrade/):
  python3 scripts/analyze_m1.py ledger      --mode m1 [--csv BTCUSDT_1m.csv]
  python3 scripts/analyze_m1.py attribution --mode m1
  python3 scripts/analyze_m1.py null|regimes|ablation|sensitivity|walkforward --mode m1
  python3 scripts/analyze_m1.py report
"""
import argparse
import csv
import json
import math
import os
import random
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from btc_robot.audit import AuditLog                       # noqa: E402
from btc_robot.backtest import compute_metrics, run_backtest  # noqa: E402
from btc_robot.config import Mode, config_hash, load_config, validate  # noqa: E402
from btc_robot.data import generate_synthetic, load_csv   # noqa: E402
from btc_robot.engine import Engine                       # noqa: E402
from btc_robot.execution import SimulatedExecutionAdapter  # noqa: E402
from btc_robot.ledger import Ledger                       # noqa: E402
from btc_robot.models import Candle, Side, Signal         # noqa: E402
from btc_robot.risk import RiskEngine                     # noqa: E402
from btc_robot.strategy import M1ScalpStrategy           # noqa: E402
from btc_robot.strategies import make_strategy           # noqa: E402
from btc_robot.validation import evaluate_holdout_once, split_holdout, walk_forward  # noqa: E402

MODES = {"m1": ("config.m1.example.json", 60_000, 0.0007, 20_000),
         "h1": ("config.h1.example.json", 3_600_000, 0.008, 6_000)}
REGIMES = ("uptrend", "downtrend", "sideways", "high_vol", "low_vol")
START_TS = 1_700_000_000_000 // 3_600_000 * 3_600_000


# ------------------------------------------------------------------ data
def scenario_candles(kind, n, seed, itv, vol_base, price0=30_000.0):
    """Random walk dengan regime terkendali. Tanpa edge yang disengaja kecuali drift tren (uptrend/downtrend)."""
    rng = random.Random(seed)
    price, vol, drift = price0, vol_base, 0.0
    if kind == "uptrend":
        drift = 0.05 * vol_base
    elif kind == "downtrend":
        drift = -0.05 * vol_base
    elif kind == "high_vol":
        vol = 3.0 * vol_base
    elif kind == "low_vol":
        vol = 0.4 * vol_base
    out = []
    for i in range(n):
        ret = rng.gauss(0, vol) + drift
        if kind == "sideways":
            ret += -0.02 * math.log(price / price0)          # tarikan balik ke rata-rata
        o = price
        c = max(1.0, o * (1 + ret))
        hi = max(o, c) * (1 + abs(rng.gauss(0, 0.25 * vol)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.25 * vol)))
        out.append(Candle(START_TS + i * itv, o, hi, lo, c, rng.uniform(50, 500)))
        price = c
    return out


def get_cfg(mode):
    return validate(load_config(os.path.join(ROOT, MODES[mode][0])))


def diag_cfg(cfg):
    """KONFIGURASI DIAGNOSTIK untuk perbandingan: batas drawdown/rugi harian dilonggarkan agar semua varian
    memproses horizon data yang SAMA (di konfigurasi asli, halt drawdown memotong run lebih awal)."""
    return replace(cfg, risk=replace(cfg.risk, max_drawdown_pct=50.0, max_daily_loss_pct=10.0))


def get_candles(args, cfg):
    if args.csv:
        return load_csv(args.csv)
    _, itv, vol, n = MODES[args.mode]
    return generate_synthetic(args.n or n, args.seed, itv, start_ts=START_TS, vol=vol)


# ------------------------------------------------------------------ engine helper (strategi/adapter kustom)
def run_custom(candles, cfg, strategy=None, adapter_cls=SimulatedExecutionAdapter, adapter_kwargs=None):
    strategy = strategy or make_strategy(cfg)
    strategy.prepare(candles)
    risk = RiskEngine(cfg, Mode.BACKTEST)
    adapter = adapter_cls(cfg.costs, Mode.BACKTEST, risk.verify, **(adapter_kwargs or {}))
    ledger = Ledger(cfg.initial_equity)
    audit = AuditLog(None, config_hash(cfg))
    eng = Engine(cfg, Mode.BACKTEST, strategy, risk, adapter, ledger, audit)
    for i in range(len(candles)):
        eng.process_bar(candles, i)
    eng.finalize(candles)
    return ledger.trades, eng, audit


# ------------------------------------------------------------------ dekomposisi biaya (inversi model adverse, eksak)
def decompose(trades, cfg):
    b = (cfg.costs.spread_bps / 2.0 + cfg.costs.slippage_bps) / 10_000.0
    rows = []
    for t in trades:
        if t["side"] != "LONG":
            continue
        entry_ref = t["entry_price"] / (1 + b)
        exit_ref = t["exit_price"] if t["exit_reason"] == "take_profit" else t["exit_price"] / (1 - b)
        gross_ref = (exit_ref - entry_ref) * t["qty"]
        rows.append({"gross_ref": gross_ref, "spread_slip": gross_ref - t["gross_pnl"], "fees": t["fees"],
                     "net": t["net_pnl"], "entry_ref": entry_ref, "exit_ref": exit_ref})
    return rows


def summarize(trades, cfg):
    d = decompose(trades, cfg)
    n = len(d)
    wins = [x["net"] for x in d if x["net"] > 0]
    losses = [x["net"] for x in d if x["net"] <= 0]
    s = lambda k: sum(x[k] for x in d)  # noqa: E731
    return {"trades": n, "win_rate": (len(wins) / n if n else 0.0),
            "gross_before_costs": s("gross_ref"), "spread_slippage": s("spread_slip"), "fees": s("fees"),
            "net": s("net"), "avg_win": (sum(wins) / len(wins) if wins else 0.0),
            "avg_loss": (sum(losses) / len(losses) if losses else 0.0)}


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, default=str)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ bagian-bagian
def sec_ledger(args):
    cfg = get_cfg(args.mode)
    candles = get_candles(args, cfg)
    res = run_backtest(candles, cfg)
    ev = res.audit.entries
    itv = cfg.interval_ms
    signals = {e["payload"]["signal"]["bar_ts"]: e["payload"]["signal"] for e in ev if e["event"] == "signal"}
    entries = [e for e in ev if e["event"] == "entry"]
    b = (cfg.costs.spread_bps / 2.0 + cfg.costs.slippage_bps) / 10_000.0
    rt_cost_pct = 2.0 * (cfg.costs.fee_rate * 100 + (cfg.costs.spread_bps / 2 + cfg.costs.slippage_bps) / 100)
    path = os.path.join(args.out, f"{args.mode}_trades.csv")
    os.makedirs(args.out, exist_ok=True)
    cols = ["no", "entry_time_utc", "exit_time_utc", "side", "signal_reason", "entry_ref_open", "entry_fill",
            "exit_ref", "exit_fill", "exit_reason", "qty", "hold_minutes", "gross_before_costs", "spread_slippage_cost",
            "fees", "net_pnl", "planned_stop", "planned_tp", "planned_stop_pct", "roundtrip_cost_pct",
            "cost_to_stop_ratio"]
    rows = []
    for no, (t, en) in enumerate(zip(res.trades, entries), 1):
        sig = signals.get(en["ts"] - itv, {})
        entry_ref = t["entry_price"] / (1 + b)
        exit_ref = t["exit_price"] if t["exit_reason"] == "take_profit" else t["exit_price"] / (1 - b)
        gross_ref = (exit_ref - entry_ref) * t["qty"]
        stop = sig.get("stop_price")
        stop_pct = (abs(sig["ref_price"] - stop) / sig["ref_price"] * 100) if stop else None
        rows.append([no, iso(t["entry_ts"]), iso(t["exit_ts"]), t["side"], sig.get("reason", ""),
                     f"{entry_ref:.2f}", f"{t['entry_price']:.2f}", f"{exit_ref:.2f}", f"{t['exit_price']:.2f}",
                     t["exit_reason"], f"{t['qty']:.5f}", f"{(t['exit_ts'] - t['entry_ts']) / 60000:.1f}",
                     f"{gross_ref:.4f}", f"{gross_ref - t['gross_pnl']:.4f}", f"{t['fees']:.4f}",
                     f"{t['net_pnl']:.4f}", f"{stop:.2f}" if stop else "", f"{sig.get('take_profit', 0):.2f}",
                     f"{stop_pct:.4f}" if stop_pct else "", f"{rt_cost_pct:.4f}",
                     f"{rt_cost_pct / stop_pct:.2f}" if stop_pct else ""])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)
    write_json(os.path.join(args.out, f"{args.mode}_backtest_metrics.json"),
               {"metrics": res.metrics, "counters": res.counters, "result_hash": res.result_hash,
                "config_hash": res.config_hash, "bars": len(candles),
                "source": args.csv or f"sintetis n={len(candles)} seed={args.seed}"})
    print(f"{path}: {len(rows)} transaksi | net={res.metrics['net_pnl']:.2f} | hash={res.result_hash[:12]}")


def sec_attribution(args):
    cfg = get_cfg(args.mode)
    candles = get_candles(args, cfg)
    res = run_backtest(candles, cfg)
    s = summarize(res.trades, cfg)
    by_reason = {}
    for t, d in zip([x for x in res.trades if x["side"] == "LONG"], decompose(res.trades, cfg)):
        r = by_reason.setdefault(t["exit_reason"], {"n": 0, "net": 0.0, "hold_min": 0.0, "gross_ref": 0.0})
        r["n"] += 1
        r["net"] += d["net"]
        r["gross_ref"] += d["gross_ref"]
        r["hold_min"] += (t["exit_ts"] - t["entry_ts"]) / 60000
    for r in by_reason.values():
        r["avg_net"], r["avg_hold_min"] = r["net"] / r["n"], r["hold_min"] / r["n"]
    ev = res.audit.entries
    sigs = {e["payload"]["signal"]["bar_ts"]: e["payload"]["signal"] for e in ev if e["event"] == "signal"}
    stop_pcts = [abs(x["ref_price"] - x["stop_price"]) / x["ref_price"] * 100 for x in sigs.values() if x.get("stop_price")]
    tp_pcts = [abs(x["take_profit"] - x["ref_price"]) / x["ref_price"] * 100 for x in sigs.values() if x.get("take_profit")]
    rt = 2.0 * (cfg.costs.fee_rate * 100 + (cfg.costs.spread_bps / 2 + cfg.costs.slippage_bps) / 100)
    avg_stop = sum(stop_pcts) / len(stop_pcts) if stop_pcts else 0.0
    avg_tp = sum(tp_pcts) / len(tp_pcts) if tp_pcts else 0.0
    p0 = avg_stop / (avg_stop + avg_tp) if (avg_stop + avg_tp) else 0.0       # P(TP sebelum SL) pada random walk
    be_wr = (avg_stop + rt) / (avg_tp + avg_stop) if (avg_tp + avg_stop) else 0.0   # win rate impas (setelah biaya)
    end = [e for e in ev if e["event"] == "backtest_end"][-1]["payload"]
    last_exit_bar = (res.trades[-1]["exit_ts"] - candles[0].ts) // cfg.interval_ms if res.trades else 0
    out = {"summary": s, "by_exit_reason": by_reason,
           "run_end": {"halted": end["halted"], "halt_reason": end["halt_reason"], "bars_total": len(candles),
                       "last_trade_exit_bar": int(last_exit_bar)},
           "structure": {"avg_stop_pct": avg_stop, "avg_tp_pct": avg_tp, "roundtrip_cost_pct": rt,
                         "cost_to_stop_ratio": (rt / avg_stop if avg_stop else None),
                         "p_tp_before_sl_random_walk": p0, "breakeven_win_rate_after_costs": be_wr,
                         "ev_pct_per_trade_random_walk": p0 * (avg_tp - rt) - (1 - p0) * (avg_stop + rt),
                         "actual_win_rate": s["win_rate"]},
           "counters": {k: v for k, v in res.counters.items() if not k.startswith("no_trade")}}
    write_json(os.path.join(args.out, f"{args.mode}_attribution.json"), out)
    print(json.dumps(out["structure"], indent=1)); print(json.dumps(s, indent=1))


class RandomEntry(M1ScalpStrategy):
    """Pembanding: masuk LONG pada bar acak (peluang p) dengan stop/TP/filter ATR yang SAMA dengan M1."""
    VERSION = "null_random_entry/1.0"

    def __init__(self, cfg, p, seed):
        super().__init__(cfg)
        self.p, self.rng = p, random.Random(seed)

    def signal_at(self, i):
        c = self._candles[i]
        a = self._atr[i]
        draw = self.rng.random()                      # selalu mengonsumsi satu undian per bar (deterministik)
        if a is None or a <= 0 or draw >= self.p:
            return self._no(i, "null_tidak_masuk")
        if self.cfg.min_atr_pct > 0 and a / c.close * 100.0 < self.cfg.min_atr_pct:
            return self._no(i, "atr_pct_rendah")
        sd = self.cfg.atr_stop_mult * a
        return Signal(c.ts, Side.LONG, "null_random", c.close, c.close - sd, c.close + self.cfg.reward_risk * sd,
                      None, self.VERSION, self._fp)


def sec_null(args):
    cfg = diag_cfg(get_cfg("m1"))
    candles = get_candles(args, cfg)
    res = run_backtest(candles, cfg)
    n_long_sig = sum(1 for e in res.audit.entries if e["event"] == "signal" and e["payload"]["signal"]["side"] == "LONG")
    p = n_long_sig / len(candles)
    m1 = summarize(res.trades, cfg)
    nets, gross, per_net, per_gross, wr, ntr = [], [], [], [], [], []
    for s in range(args.null_runs):
        tr, _, _ = run_custom(candles, cfg, RandomEntry(cfg.strategy, p, 1000 + s))
        z = summarize(tr, cfg)
        nets.append(z["net"]); gross.append(z["gross_before_costs"]); ntr.append(z["trades"]); wr.append(z["win_rate"])
        per_net.append(z["net"] / z["trades"] if z["trades"] else 0.0)
        per_gross.append(z["gross_before_costs"] / z["trades"] if z["trades"] else 0.0)
    nets_sorted = sorted(nets)
    pct = sum(1 for x in nets if x <= m1["net"]) / len(nets)
    m1_pg = m1["gross_before_costs"] / m1["trades"] if m1["trades"] else 0.0
    out = {"config": "diagnostik tanpa halt drawdown (horizon data sama)", "bars": len(candles),
           "p_entry_per_bar": p, "null_runs": len(nets), "m1_net": m1["net"],
           "m1_gross_before_costs": m1["gross_before_costs"], "m1_trades": m1["trades"], "m1_win_rate": m1["win_rate"],
           "m1_gross_per_trade": m1_pg, "m1_net_per_trade": m1["net"] / m1["trades"] if m1["trades"] else 0.0,
           "null_trades_mean": sum(ntr) / len(ntr), "null_win_rate_mean": sum(wr) / len(wr),
           "null_net_mean": sum(nets) / len(nets), "null_net_p05": nets_sorted[int(0.05 * len(nets))],
           "null_net_p50": nets_sorted[len(nets) // 2], "null_net_p95": nets_sorted[int(0.95 * len(nets)) - 1],
           "null_gross_mean": sum(gross) / len(gross),
           "null_gross_per_trade_mean": sum(per_gross) / len(per_gross),
           "null_net_per_trade_mean": sum(per_net) / len(per_net),
           "m1_percentile_net_among_null": pct,
           "m1_percentile_gross_per_trade_among_null": sum(1 for x in per_gross if x <= m1_pg) / len(per_gross)}
    write_json(os.path.join(args.out, "m1_null_baseline.json"), out)
    print(json.dumps(out, indent=1))


def run_regime(mode, kind, seed, cfg=None, n=None):
    cfg = diag_cfg(cfg or get_cfg(mode))
    _, itv, vol, nn = MODES[mode]
    c = scenario_candles(kind, n or nn, seed, itv, vol)
    tr, eng, _ = run_custom(c, cfg)
    return summarize(tr, cfg)


def sec_regimes(args):
    seeds = [int(x) for x in args.seeds.split(",")]
    res = {}
    for kind in REGIMES:
        runs = [run_regime(args.mode, kind, s, n=args.n) for s in seeds]
        agg = {k: sum(r[k] for r in runs) for k in ("trades", "gross_before_costs", "spread_slippage", "fees", "net")}
        agg["win_rate"] = (sum(r["win_rate"] * r["trades"] for r in runs) / agg["trades"]) if agg["trades"] else 0.0
        agg["seeds"] = seeds
        res[kind] = agg
        print(kind, {k: round(v, 3) if isinstance(v, float) else v for k, v in agg.items() if k != "seeds"}, flush=True)
    write_json(os.path.join(args.out, f"{args.mode}_regimes.json"), res)


def variants(cfg):
    st, rk, co = cfg.strategy, cfg.risk, cfg.costs
    return {
        "baseline": cfg,
        "no_trailing": replace(cfg, risk=replace(rk, trailing_trail_pct=0.0)),
        "no_max_hold": replace(cfg, risk=replace(rk, max_hold_sec=0)),
        "no_cooldown": replace(cfg, risk=replace(rk, max_loss_streak=100_000)),
        "no_edge_filter": replace(cfg, risk=replace(rk, min_edge_cost_mult=0.0)),
        "no_rsi_filter": replace(cfg, strategy=replace(st, rsi_long_min=0.0, rsi_long_max=100.0)),
        "wider_stop_3atr": replace(cfg, strategy=replace(st, atr_stop_mult=3.0)),
        "zero_cost": replace(cfg, costs=replace(co, fee_rate=0.0, spread_bps=0.0, slippage_bps=0.0),
                             risk=replace(rk, min_edge_cost_mult=0.0)),
    }


def sec_ablation(args):
    seeds = [int(x) for x in args.seeds.split(",")]
    cfg0 = diag_cfg(get_cfg(args.mode))
    _, itv, vol, nn = MODES[args.mode]
    out = {}
    for name, cfg in variants(cfg0).items():
        runs = []
        for s in seeds:
            c = generate_synthetic(args.n or nn, s, itv, start_ts=START_TS, vol=vol)
            tr, _, _ = run_custom(c, validate(cfg))
            runs.append(summarize(tr, cfg))
        agg = {k: sum(r[k] for r in runs) for k in ("trades", "gross_before_costs", "spread_slippage", "fees", "net")}
        agg["seeds"] = seeds
        out[name] = agg
        print(f"{name:16s} trades={agg['trades']:4d} gross={agg['gross_before_costs']:8.2f} costs={agg['spread_slippage'] + agg['fees']:8.2f} net={agg['net']:8.2f}", flush=True)
    write_json(os.path.join(args.out, f"{args.mode}_ablation.json"), out)


class PenetrationAdapter(SimulatedExecutionAdapter):
    """Sensitivitas: TP baru terisi bila harga MENEMBUS TP sebesar pen_bps (model antrean yang lebih realistis)."""
    def __init__(self, costs, mode, verify, pen_bps=0.0):
        super().__init__(costs, mode, verify)
        self.pen = pen_bps / 10_000.0

    def check_exit(self, pos, bar):
        if pos.take_profit is not None and self.pen > 0:
            need = pos.take_profit * (1 + self.pen)
            hit_tp = bar.high >= need
            stop_hit = bar.low <= pos.stop_price
            if not stop_hit and not hit_tp:
                return None
            if not stop_hit and hit_tp:
                return super().check_exit(pos, bar)
            if stop_hit:
                return super().check_exit(pos, bar)
        return super().check_exit(pos, bar)


def sec_sensitivity(args):
    seeds = [int(x) for x in args.seeds.split(",")]
    cfg0 = diag_cfg(get_cfg(args.mode))
    _, itv, vol, nn = MODES[args.mode]
    data = [generate_synthetic(args.n or nn, s, itv, start_ts=START_TS, vol=vol) for s in seeds]
    out = {"tp_touch_penetration_bps": {}, "cost_multiplier": {}}
    for pen in (0.0, 1.0, 2.0, 5.0):
        net = tr_n = 0
        for c in data:
            tr, _, _ = run_custom(c, cfg0, adapter_cls=PenetrationAdapter, adapter_kwargs={"pen_bps": pen})
            z = summarize(tr, cfg0)
            net += z["net"]; tr_n += z["trades"]
        out["tp_touch_penetration_bps"][str(pen)] = {"net": net, "trades": tr_n}
        print("penetrasi TP", pen, "bps -> net", round(net, 2), "trades", tr_n, flush=True)
    for mult in (0.5, 1.0, 2.0):
        cfg = replace(cfg0, costs=replace(cfg0.costs, fee_rate=cfg0.costs.fee_rate * mult,
                                          spread_bps=cfg0.costs.spread_bps * mult,
                                          slippage_bps=cfg0.costs.slippage_bps * mult))
        net = tr_n = 0
        for c in data:
            tr, _, _ = run_custom(c, cfg)
            z = summarize(tr, cfg)
            net += z["net"]; tr_n += z["trades"]
        out["cost_multiplier"][str(mult)] = {"net": net, "trades": tr_n}
        print("biaya x", mult, "-> net", round(net, 2), "trades", tr_n, flush=True)
    write_json(os.path.join(args.out, f"{args.mode}_sensitivity.json"), out)


def sec_walkforward(args):
    cfg = get_cfg(args.mode)
    candles = get_candles(args, cfg)
    train, hold = split_holdout(candles, 0.2)
    wf = walk_forward(train, cfg, n_windows=4)
    marker = os.path.join(tempfile.mkdtemp(), "holdout_marker.json")
    hm = evaluate_holdout_once(hold, cfg, marker)
    out = {"source": args.csv or f"sintetis n={len(candles)} seed={args.seed}",
           "parameters_fixed_a_priori": True, "holdout_used_for_selection": False,
           "walk_forward_windows": wf, "holdout": hm,
           "costs": {"fee_rate": cfg.costs.fee_rate, "spread_bps": cfg.costs.spread_bps,
                     "slippage_bps": cfg.costs.slippage_bps}}
    write_json(os.path.join(args.out, f"{args.mode}_walkforward_holdout.json"), out)
    for w in wf:
        print("WF", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in w.items()})
    print("HOLDOUT", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in hm.items() if k != "disclaimer"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("section", choices=["ledger", "attribution", "null", "regimes", "ablation", "sensitivity",
                                        "walkforward"])
    ap.add_argument("--mode", choices=list(MODES), default="m1")
    ap.add_argument("--csv")
    ap.add_argument("--n", type=int)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--seeds", default="1,2,3")
    ap.add_argument("--null-runs", type=int, default=60)
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "m1_audit"))
    args = ap.parse_args()
    {"ledger": sec_ledger, "attribution": sec_attribution, "null": sec_null, "regimes": sec_regimes,
     "ablation": sec_ablation, "sensitivity": sec_sensitivity, "walkforward": sec_walkforward}[args.section](args)


if __name__ == "__main__":
    main()
