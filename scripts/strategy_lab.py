#!/usr/bin/env python3
"""Uji banyak strategi pada BTC/BNB/SOL di M15 dan H1: backtest penuh + walk-forward 4 jendela.

Parameter strategi TETAP (tidak dioptimasi). Biaya: fee + spread + slippage, dihitung mesin backtest.

Langkah (butuh internet, di HP/PC Anda):
  python3 scripts/fetch_all.py --years 3            # unduh 3 simbol x 2 timeframe ke data/
  python3 scripts/strategy_lab.py --data data       # jalankan semua uji
Atau uji mesin saja dengan data sintetis (tidak bermakna untuk profit):
  python3 scripts/strategy_lab.py --synthetic

Keluaran: tabel di layar + docs/strategy_lab/results.json + results.md
Kriteria 'LOLOS KANDIDAT' (belum bukti profit): total return > 0, >= 30 transaksi, profit factor >= 1.15,
dan >= 3 dari 4 jendela walk-forward positif. Ini saringan awal, bukan izin live.
"""
import argparse
import json
import os
import sys
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from btc_robot.backtest import run_backtest                 # noqa: E402
from btc_robot.config import load_config, validate          # noqa: E402
from btc_robot.data import generate_synthetic, load_csv     # noqa: E402
from btc_robot.validation import walk_forward               # noqa: E402

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
TFS = {"15m": ("config.m15.example.json", 900_000), "1h": ("config.h1.example.json", 3_600_000),
       "4h": ("config.h1.example.json", 14_400_000), "1d": ("config.h1.example.json", 86_400_000)}
STRATS = {
    "15m": ["ema_rsi", "mtf_1h_30m_15m", "lab_pullback", "lab_fib", "lab_donchian", "lab_bbrev"],
    "1h": ["ema_rsi", "h1_trend", "lab_pullback", "lab_fib", "lab_donchian", "lab_bbrev", "lab_trend"],
    "4h": ["ema_rsi", "lab_trend", "lab_donchian", "lab_pullback", "lab_fib"],
    "1d": ["ema_rsi", "lab_trend", "lab_donchian", "lab_pullback", "lab_fib"],
}


def build_cfg(tf, symbol, mode, fee, allow_short):
    base = load_config(os.path.join(ROOT, TFS[tf][0]))
    strat = base.strategy
    if mode.startswith("lab_"):
        strat = replace(strat, atr_stop_mult=1.5 if tf == "15m" else 2.0, reward_risk=2.0)
    costs = replace(base.costs, fee_rate=fee, spread_bps=1.0, slippage_bps=1.0)
    risk = replace(base.risk, allow_short=allow_short)
    if tf in ("4h", "1d"):  # swing/tahan lama: stop lebar, risiko 2%/transaksi (batas maksimum robot)
        ims = TFS[tf][1] // 1000
        risk = replace(risk, risk_per_trade_pct=2.0, max_stop_distance_pct=20.0, min_stop_distance_pct=0.5,
                       max_hold_sec=30 * 86_400, loss_streak_cooldown_sec=2 * ims, max_data_age_seconds=3 * ims,
                       trailing_trail_pct=(8.0 if tf == "1d" else 5.0) if mode == "lab_trend" else 0.0,
                       trailing_activation_pct=3.0 if mode == "lab_trend" else 1.0)
    elif mode == "lab_trend":
        risk = replace(risk, trailing_trail_pct=3.0, trailing_activation_pct=1.5)
    if tf in ("4h", "1d") and mode.startswith("lab_"):
        strat = replace(strat, atr_stop_mult=2.5 if mode == "lab_trend" else 2.0, reward_risk=2.0)
    return validate(replace(base, symbol=symbol, interval=tf, strategy_mode=mode, strategy=strat, costs=costs, risk=risk,
                            state_dir=os.path.join(ROOT, "state", "lab")))


def evaluate(candles, cfg):
    res = run_backtest(candles, cfg)
    m = res.metrics
    row = {"trades": m["trades"], "return_pct": m["return_pct"], "max_dd_pct": m["max_drawdown_pct"],
           "win_rate": m["win_rate"], "profit_factor": m["profit_factor"], "avg_trade": m["avg_trade"],
           "fees": m["total_fees"], "halted_by_risk": res.counters.get("halted", 0) if res.counters else 0}
    for side in ("LONG", "SHORT"):
        ts_ = [t for t in res.trades if t["side"] == side]
        row[f"{side.lower()}_n"] = len(ts_)
        row[f"{side.lower()}_pnl_pct"] = sum(t["net_pnl"] for t in ts_) / cfg.initial_equity * 100
    try:
        wf = walk_forward(candles, cfg, n_windows=4)
        row["wf_returns_pct"] = [round(w["return_pct"], 2) for w in wf]
        row["wf_trades"] = [w["trades"] for w in wf]
        row["wf_positive"] = sum(1 for w in wf if w["net_pnl"] > 0)
    except Exception as exc:  # data pendek dll
        row["wf_error"] = str(exc)
        row["wf_positive"] = 0
    pf = row["profit_factor"]
    row["candidate"] = bool(row["return_pct"] > 0 and row["trades"] >= 30 and pf is not None
                            and pf >= 1.15 and row["wf_positive"] >= 3)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data", help="folder berisi BTCUSDT_15m.csv, BNBUSDT_1h.csv, dst.")
    ap.add_argument("--synthetic", action="store_true", help="data sintetis (hanya uji mesin)")
    ap.add_argument("--fee", type=float, default=0.00075, help="fee per sisi (0.00075 = 0.075%%)")
    ap.add_argument("--allow-short", action="store_true", help="izinkan short (futures); default spot long-only")
    ap.add_argument("--tfs", default="15m,1h,4h,1d", help="daftar timeframe, dipisah koma")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "strategy_lab"))
    a = ap.parse_args()

    results = []
    for sym in SYMBOLS:
        for tf, (_, ms) in TFS.items():
            if tf not in a.tfs.split(","):
                continue
            if a.synthetic:
                vol = {"15m": 0.0035, "1h": 0.008, "4h": 0.016, "1d": 0.03}[tf]
                candles = generate_synthetic({"15m": 20_000, "1h": 6_000, "4h": 6_000, "1d": 2_000}[tf],
                                             seed=SYMBOLS.index(sym) + 1, interval_ms=ms, vol=vol,
                                             start_ts=1_700_000_000_000 // ms * ms)
            else:
                path = os.path.join(a.data, f"{sym}_{tf}.csv")
                if not os.path.exists(path):
                    print(f"[lewati] {path} tidak ada")
                    continue
                candles = load_csv(path)
            bh = (candles[-1].close / candles[0].close - 1) * 100
            pk, bh_dd = 0.0, 0.0
            for cc in candles:
                pk = max(pk, cc.close); bh_dd = max(bh_dd, (pk - cc.close) / pk * 100)
            for mode in STRATS[tf]:
                cfg = build_cfg(tf, sym, mode, a.fee, a.allow_short)
                try:
                    row = evaluate(candles, cfg)
                except Exception as exc:
                    print(f"[ERROR] {sym} {tf} {mode}: {exc}")
                    row = {"error": str(exc), "candidate": False, "trades": 0, "return_pct": 0.0,
                           "wf_positive": 0, "max_dd_pct": 0.0, "profit_factor": None, "win_rate": None}
                row.update(symbol=sym, tf=tf, strategy=mode, buy_hold_pct=round(bh, 1), buy_hold_dd=round(bh_dd, 1), bars=len(candles))
                results.append(row)
                pf = row["profit_factor"]
                print(f"{sym:8} {tf:3} {mode:15} n={row['trades']:4} ret={row['return_pct']:7.2f}% "
                      f"dd={row['max_dd_pct']:5.1f}% pf={'-' if pf is None else f'{pf:.2f}'} "
                      f"wf+={row['wf_positive']}/4 | LONG n={row.get('long_n', 0)} {row.get('long_pnl_pct', 0):+.1f}% "
                      f"| SHORT n={row.get('short_n', 0)} {row.get('short_pnl_pct', 0):+.1f}% "
                      f"{'<== KANDIDAT' if row['candidate'] else ''}")
    os.makedirs(a.out, exist_ok=True)
    tag = ("synthetic" if a.synthetic else "real") + ("_short" if a.allow_short else "")
    with open(os.path.join(a.out, f"results_{tag}.json"), "w") as fh:
        json.dump(results, fh, indent=1)
    with open(os.path.join(a.out, f"results_{tag}.md"), "w") as fh:
        fh.write(f"# Hasil uji strategi ({tag}) - {'LONG + SHORT' if a.allow_short else 'LONG-ONLY'} fee={a.fee}\n\n"
                 "| Simbol | TF | Strategi | Trx | Return% | MaxDD% | PF | Win% | WF+ | LONG trx / % | SHORT trx / % | B&H% | B&H DD% | Kandidat |\n"
                 "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in results:
            pf = r["profit_factor"]
            wr = r["win_rate"]
            fh.write(f"| {r['symbol']} | {r['tf']} | {r['strategy']} | {r['trades']} | {r['return_pct']:.2f} | "
                     f"{r['max_dd_pct']:.1f} | {'-' if pf is None else f'{pf:.2f}'} | "
                     f"{'-' if wr is None else f'{wr*100:.0f}'} | {r['wf_positive']}/4 | {r.get('long_n', 0)} / {r.get('long_pnl_pct', 0):+.1f} | {r.get('short_n', 0)} / {r.get('short_pnl_pct', 0):+.1f} | {r['buy_hold_pct']} | {r['buy_hold_dd']} | "
                     f"{'YA' if r['candidate'] else ''} |\n")
    print("selesai ->", a.out)


if __name__ == "__main__":
    main()
