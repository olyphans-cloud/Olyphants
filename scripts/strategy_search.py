#!/usr/bin/env python3
"""Pencarian parameter terbaik DENGAN disiplin out-of-sample.

Data dibagi urut waktu: TRAIN 60% | VALIDASI 20% | HOLDOUT 20%.
  1. Grid parameter dievaluasi HANYA di TRAIN. Skor = return/(maxDD+1), syarat transaksi >= MIN_TRADES.
  2. 3 parameter teratas diuji di VALIDASI; yang terbaik di validasi dipilih (1 pemenang per strategi).
  3. Pemenang diuji SEKALI di HOLDOUT. Hanya angka HOLDOUT yang boleh dipercaya.
Pakai data asli:  python3 scripts/strategy_search.py --data data
Pakai sintetis :  python3 scripts/strategy_search.py --synthetic [--seeds 3]
"""
import argparse, itertools, json, os, sys
from dataclasses import replace
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "scripts"))
from btc_robot.backtest import run_backtest, max_drawdown_pct   # noqa: E402
from btc_robot.data import generate_synthetic, load_csv         # noqa: E402
from btc_robot import strategies_lab as L                       # noqa: E402
from strategy_lab import build_cfg, TFS                         # noqa: E402
from strategy_multiseed import null_walk                        # noqa: E402

WARM = 300
GRIDS = {
    "lab_donchian": dict(stop=[1.5, 2, 3], rr=[1.5, 2, 3], look=[24, 48, 96]),
    "lab_pullback": dict(stop=[1.5, 2, 3], rr=[1.5, 2, 3], ema=[(10, 30), (20, 50)]),
    "lab_fib":      dict(stop=[1.5, 2, 3], rr=[1.5, 2, 3]),
    "lab_bbrev":    dict(stop=[1.5, 2, 3], rr=[1.5, 2, 3]),
    "mtf_1h_30m_15m": dict(stop=[1.5, 2, 3], rr=[1.5, 2, 3]),
    "h1_trend":     dict(stop=[1.5, 2, 3], rr=[1.5, 2, 3]),
    "lab_trend":    dict(stop=[2.5], rr=[2]),
}
STRAT_TF = {"15m": ["lab_donchian", "lab_pullback", "lab_fib", "lab_bbrev", "mtf_1h_30m_15m"],
            "1h": ["lab_donchian", "lab_pullback", "lab_fib", "lab_bbrev", "h1_trend"],
            "4h": ["lab_donchian", "lab_pullback", "lab_fib", "lab_trend"],
            "1d": ["lab_donchian", "lab_pullback", "lab_fib", "lab_trend"]}


def combos(mode):
    g = GRIDS[mode]
    for s, r in itertools.product(g["stop"], g["rr"]):
        if "look" in g:
            for lk in g["look"]:
                yield dict(stop=s, rr=r, look=lk)
        elif "ema" in g:
            for e in g["ema"]:
                yield dict(stop=s, rr=r, ema=e)
        else:
            yield dict(stop=s, rr=r)


ALLOW_SHORT = False


def make(tf, mode, p, fee):
    cfg = build_cfg(tf, "BTCUSDT", mode, fee, ALLOW_SHORT)
    st = replace(cfg.strategy, atr_stop_mult=p["stop"], reward_risk=p["rr"])
    if "ema" in p:
        st = replace(st, ema_fast=p["ema"][0], ema_slow=p["ema"][1])
    if "look" in p:
        L.DonchianStrategy.LOOK = p["look"]
    return replace(cfg, strategy=st)


def seg_eval(candles, a, b, cfg):
    s0 = max(0, a - WARM)
    seg = candles[s0:b]
    res = run_backtest(seg, cfg)
    t0 = candles[a].ts
    tr = [t for t in res.trades if t["entry_ts"] >= t0]
    curve = [(ts, e) for ts, e in res.equity_curve if ts >= t0]
    pnl = sum(t["net_pnl"] for t in tr)
    gw = sum(t["net_pnl"] for t in tr if t["net_pnl"] > 0)
    gl = -sum(t["net_pnl"] for t in tr if t["net_pnl"] <= 0)
    ret = pnl / cfg.initial_equity * 100
    dd = max_drawdown_pct(curve) if curve else 0.0
    return dict(n=len(tr), ret=ret, dd=dd, pf=(gw / gl if gl > 0 else None))


def search(candles, tf, mode, fee, min_trades):
    n = len(candles)
    c1, c2 = int(n * .6), int(n * .8)
    train = []
    for p in combos(mode):
        cfg = make(tf, mode, p, fee)
        r = seg_eval(candles, WARM, c1, cfg)
        score = r["ret"] / (r["dd"] + 1) if r["n"] >= min_trades else -999
        train.append((score, p, r))
    train.sort(key=lambda x: -x[0])
    top = [t for t in train[:3] if t[0] > -999]
    if not top:
        return None
    val = []
    for sc, p, rt in top:
        rv = seg_eval(candles, c1, c2, make(tf, mode, p, fee))
        val.append((rv["ret"] / (rv["dd"] + 1), p, rt, rv))
    val.sort(key=lambda x: -x[0])
    _, p, rt, rv = val[0]
    rh = seg_eval(candles, c2, n, make(tf, mode, p, fee))
    L.DonchianStrategy.LOOK = 48
    return dict(strategy=mode, tf=tf, params=p, train=rt, valid=rv, holdout=rh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--fee", type=float, default=0.00075)
    ap.add_argument("--allow-short", action="store_true")
    ap.add_argument("--tfs", default="15m,1h,4h,1d")
    a = ap.parse_args()
    global ALLOW_SHORT
    ALLOW_SHORT = a.allow_short
    jobs = []
    if a.synthetic:
        for regime in ("null", "trend"):
            for tf in ("15m", "1h"):
                ms = TFS[tf][1]; n, vol = (12_000, 0.0035) if tf == "15m" else (5_000, 0.008)
                for s in range(11, 11 + a.seeds):
                    c = null_walk(n, s, ms, vol) if regime == "null" else generate_synthetic(n, s, ms, vol=vol)
                    jobs.append((f"{regime}-seed{s}", tf, c))
    else:
        for sym in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"):
            for tf in a.tfs.split(","):
                pth = os.path.join(a.data, f"{sym}_{tf}.csv")
                if os.path.exists(pth):
                    jobs.append((sym, tf, load_csv(pth)))
    out = []
    for name, tf, c in jobs:
        mt = {"15m": 30, "1h": 15, "4h": 10, "1d": 6}[tf]
        for mode in STRAT_TF[tf]:
            r = search(c, tf, mode, a.fee, mt)
            if r:
                r["dataset"] = name
                out.append(r)
                print(f"{name:12} {tf:3} {mode:15} {r['params']}  train={r['train']['ret']:6.1f}%  "
                      f"valid={r['valid']['ret']:6.1f}%  HOLDOUT={r['holdout']['ret']:6.1f}% (n={r['holdout']['n']})", flush=True)
    os.makedirs(os.path.join(ROOT, "docs", "strategy_lab"), exist_ok=True)
    tag = ("synthetic" if a.synthetic else "real") + ("_short" if a.allow_short else "")
    json.dump(out, open(os.path.join(ROOT, "docs", "strategy_lab", f"search_{tag}.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
