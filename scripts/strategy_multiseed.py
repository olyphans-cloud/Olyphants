#!/usr/bin/env python3
"""Uji tiap strategi pada banyak dataset sintetis, dua rezim:
  null  = random walk TANPA drift/tren (kontrol: strategi tanpa edge harus ~ -biaya; kalau untung = untung-untungan)
  trend = generate_synthetic bawaan (tren persisten ~200 candle)
Laporan per strategi: rata-rata return, % dataset untung, rata-rata PF, rata-rata transaksi, rata-rata jendela WF positif.
Pakai: python3 scripts/strategy_multiseed.py --seeds 6
"""
import argparse, json, os, random, sys
from dataclasses import replace
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from btc_robot.data import generate_synthetic          # noqa: E402
from btc_robot.models import Candle                    # noqa: E402
from strategy_lab import build_cfg, evaluate, STRATS, TFS   # noqa: E402


def null_walk(n, seed, ms, vol):
    rng = random.Random(seed)
    price, v, out = 30_000.0, vol, []
    t0 = 1_700_000_000_000 // 3_600_000 * 3_600_000
    for i in range(n):
        v = max(vol * 0.4, min(vol * 2.5, 0.95 * v + 0.05 * vol + 0.15 * vol * rng.gauss(0, 1)))  # volatilitas bergerombol
        r = rng.gauss(0, v)
        o, c = price, price * (1 + r)
        hi = max(o, c) * (1 + abs(rng.gauss(0, v * 0.3)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, v * 0.3)))
        out.append(Candle(t0 + i * ms, o, hi, lo, c, rng.uniform(50, 500)))
        price = c
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=6)
    ap.add_argument("--fee", type=float, default=0.00075)
    a = ap.parse_args()
    allstr = ["ema_rsi", "mtf_1h_30m_15m", "h1_trend", "lab_pullback", "lab_fib", "lab_donchian", "lab_bbrev", "lab_rsi2"]
    rows = []
    for regime in ("null", "trend"):
        for tf in ("15m", "1h"):
            ms = TFS[tf][1]
            n, vol = (12_000, 0.0035) if tf == "15m" else (5_000, 0.008)
            data = [(null_walk(n, s, ms, vol) if regime == "null" else generate_synthetic(n, s, ms, vol=vol))
                    for s in range(1, a.seeds + 1)]
            for mode in [m for m in allstr if m in STRATS[tf] or m.startswith("lab_")]:
                rs = [evaluate(c, build_cfg(tf, "BTCUSDT", mode, a.fee, False)) for c in data]
                pfs = [r["profit_factor"] for r in rs if r["profit_factor"] is not None]
                row = dict(regime=regime, tf=tf, strategy=mode,
                           mean_return=sum(r["return_pct"] for r in rs) / len(rs),
                           pct_profitable=100 * sum(r["return_pct"] > 0 for r in rs) / len(rs),
                           mean_pf=sum(pfs) / len(pfs) if pfs else None,
                           mean_trades=sum(r["trades"] for r in rs) / len(rs),
                           mean_wf_pos=sum(r["wf_positive"] for r in rs) / len(rs),
                           worst_dd=max(r["max_dd_pct"] for r in rs))
                rows.append(row)
                print(regime, tf, f"{mode:15}", f"ret={row['mean_return']:7.2f}% win={row['pct_profitable']:4.0f}% "
                      f"pf={row['mean_pf'] or 0:.2f} n={row['mean_trades']:.0f} wf+={row['mean_wf_pos']:.1f}/4", flush=True)
    os.makedirs(os.path.join(ROOT, "docs", "strategy_lab"), exist_ok=True)
    json.dump(rows, open(os.path.join(ROOT, "docs", "strategy_lab", "multiseed.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
