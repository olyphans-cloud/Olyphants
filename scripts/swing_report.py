#!/usr/bin/env python3
"""Laporan swing 4h/1d: SPOT (long-only) vs FUTURES (long+short, tanpa leverage) memakai strategi per config (4h: d1h4, 1d: lab_trend).

Biaya funding futures TIDAK dimodelkan di mesin; di sini dipotong PASCA-HOC secara konservatif:
funding_rate per 8 jam x lama tahan x notional, dikenakan pada long MAUPUN short (anggap selalu membayar).

  python3 scripts/swing_report.py --synthetic             # uji mesin saja (tidak bermakna untuk profit)
  python3 scripts/swing_report.py --data data             # data nyata: BTCUSDT_4h.csv, BTCUSDT_1d.csv, dst.
Keluaran: docs/swing/REPORT.md. Bukan bukti profit; semua gerbang G0-G8 tetap berlaku.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from btc_robot.backtest import run_backtest              # noqa: E402
from btc_robot.config import load_config, validate       # noqa: E402
from dataclasses import replace                          # noqa: E402
from btc_robot.data import generate_synthetic, load_csv  # noqa: E402
from btc_robot.validation import walk_forward            # noqa: E402

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
MS = {"4h": 14_400_000, "1d": 86_400_000}
VOL = {"4h": 0.016, "1d": 0.03}
N = {"4h": 6_000, "1d": 2_000}


def funding_cost(trades, rate_8h):
    tot = 0.0
    for t in trades:
        hours = (t["exit_ts"] - t["entry_ts"]) / 3_600_000.0
        tot += t["qty"] * t["entry_price"] * rate_8h * (hours / 8.0)
    return tot


def run(market, tf, sym, candles, rate_8h):
    cfg = load_config(os.path.join(ROOT, f"config.{market}_{tf}.example.json"))
    cfg = validate(replace(cfg, symbol=sym, state_dir=os.path.join(ROOT, "state", "report")))
    res = run_backtest(candles, cfg)
    m, eq0 = res.metrics, cfg.initial_equity
    fund = funding_cost(res.trades, rate_8h) if market == "futures" else 0.0
    row = {"n": m["trades"], "ret": m["return_pct"] - fund / eq0 * 100, "dd": m["max_drawdown_pct"],
           "pf": m["profit_factor"], "fund": fund / eq0 * 100}
    for side in ("LONG", "SHORT"):
        ts_ = [t for t in res.trades if t["side"] == side]
        row[side] = (len(ts_), sum(t["net_pnl"] for t in ts_) / eq0 * 100)
    try:
        wf = walk_forward(candles, cfg, n_windows=4)
        row["wf"] = sum(1 for w in wf if w["net_pnl"] > 0)
    except Exception:
        row["wf"] = "-"
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--funding-8h", type=float, default=0.0001, help="0.0001 = 0.01%% per 8 jam")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "swing"))
    a = ap.parse_args()
    lines = ["# Laporan swing 4h/1d: SPOT vs FUTURES (4h: d1h4_spot/d1h4_futures, 1d: lab_trend)",
             f"Sumber: {'SINTETIS (tidak bermakna untuk profit)' if a.synthetic else 'data nyata'}; "
             f"funding {a.funding_8h*100:.3f}%/8j dipotong pasca-hoc di futures.", "",
             "| Simbol | TF | Pasar | Trx | Return% | MaxDD% | PF | WF+ | LONG trx/% | SHORT trx/% | Funding% |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for sym in SYMBOLS:
        for tf in ("4h", "1d"):
            if a.synthetic:
                c = generate_synthetic(N[tf], seed=SYMBOLS.index(sym) + 1, interval_ms=MS[tf], vol=VOL[tf],
                                       start_ts=1_700_000_000_000 // MS[tf] * MS[tf])
            else:
                p = os.path.join(a.data, f"{sym}_{tf}.csv")
                if not os.path.exists(p):
                    print(f"[lewati] {p}")
                    continue
                c = load_csv(p)
            for market in ("spot", "futures"):
                r = run(market, tf, sym, c, a.funding_8h)
                pf = "-" if r["pf"] is None else f"{r['pf']:.2f}"
                lines.append(f"| {sym} | {tf} | {market} | {r['n']} | {r['ret']:.2f} | {r['dd']:.1f} | {pf} | {r['wf']}/4 | "
                             f"{r['LONG'][0]} / {r['LONG'][1]:+.1f} | {r['SHORT'][0]} / {r['SHORT'][1]:+.1f} | {r['fund']:.2f} |")
                print(lines[-1])
    os.makedirs(a.out, exist_ok=True)
    name = "REPORT_synthetic.md" if a.synthetic else "REPORT.md"
    with open(os.path.join(a.out, name), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("selesai ->", os.path.join(a.out, name))


if __name__ == "__main__":
    main()
