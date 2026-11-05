#!/usr/bin/env python3
"""Satu perintah: unduh data (jika belum ada) -> backtest swing 4h/1d spot & futures -> laporan .txt.

  python3 scripts/auto_report.py                      # data nyata (butuh internet)
  python3 scripts/auto_report.py --symbols BTCUSDT    # hanya BTC (lebih cepat di HP)
  python3 scripts/auto_report.py --synthetic          # uji mesin tanpa internet
Laporan: docs/swing/REPORT_terbaru.txt (+ salinan bertanggal), dan disalin ke ~/storage/downloads bila ada (Termux).
Saringan awal di laporan HANYA penyaring kasar, bukan gerbang G0-G8 dan bukan izin LIVE.
"""
import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import swing_report as sr                                   # noqa: E402
from btc_robot.data import generate_synthetic, load_csv     # noqa: E402

STRAT = {("spot", "4h"): "d1h4_spot", ("futures", "4h"): "d1h4_futures",
         ("spot", "1d"): "lab_trend", ("futures", "1d"): "lab_trend"}


def ensure_data(sym, tf, years, out_dir, refresh):
    path = os.path.join(out_dir, f"{sym}_{tf}.csv")
    if not refresh and os.path.exists(path) and os.path.getsize(path) > 1000:
        return path, None
    os.makedirs(out_dir, exist_ok=True)
    r = subprocess.run([sys.executable, os.path.join(HERE, "download_btc_history.py"), "--symbol", sym,
                        "--interval", tf, "--years", str(years), "--out", path],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) < 1000:
        return None, (r.stderr or r.stdout or "unduhan gagal").strip().splitlines()[-1][:160]
    return path, None


def verdict(r, tf):
    why = []
    need = 30 if tf == "4h" else 15
    if r["n"] < need:
        why.append(f"transaksi hanya {r['n']} (<{need}, belum cukup bukti)")
    if r["ret"] <= 0:
        why.append("return <= 0")
    if r["pf"] is None or r["pf"] < 1.2:
        why.append("PF < 1.2")
    if r["dd"] > 15:
        why.append("drawdown > 15%")
    if r["wf"] != "-" and r["wf"] < 3:
        why.append(f"walk-forward {r['wf']}/4 (<3)")
    return ("LOLOS saringan awal" if not why else "BELUM: " + "; ".join(why)), not why


def fmt_row(label, r):
    pf = "-" if r["pf"] is None else f"{r['pf']:.2f}"
    s = (f"{label:<8}: trx {r['n']:>3} | return {r['ret']:+7.2f}% | maxDD {r['dd']:5.1f}% | PF {pf:>5} | "
         f"WF {r['wf']}/4")
    if label == "FUTURES":
        s += (f"\n          LONG {r['LONG'][0]} trx ({r['LONG'][1]:+.1f}%) | SHORT {r['SHORT'][0]} trx "
              f"({r['SHORT'][1]:+.1f}%) | funding -{r['fund']:.2f}%")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--data", default=os.path.join(ROOT, "data"))
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="unduh ulang data")
    ap.add_argument("--funding-8h", type=float, default=0.0001)
    a = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    L = ["LAPORAN SWING olypahantstrade v5-rc10 (4h & 1d, SPOT & FUTURES)",
         f"Dibuat : {now:%Y-%m-%d %H:%M} UTC",
         f"Sumber : {'SINTETIS - hanya uji mesin, BUKAN hasil pasar' if a.synthetic else 'data nyata Binance'}",
         f"Biaya  : fee/spread/slippage dari config; funding futures {a.funding_8h*100:.3f}%/8 jam dipotong pasca-hoc",
         "Risiko : 1% per transaksi, tanpa leverage, risk engine robot aktif", ""]
    passed, notes, results = [], [], 0
    for sym in [s.strip().upper() for s in a.symbols.split(",") if s.strip()]:
        for tf in ("4h", "1d"):
            if a.synthetic:
                ms = sr.MS[tf]
                c = generate_synthetic(sr.N[tf], seed=sr.SYMBOLS.index(sym) + 1 if sym in sr.SYMBOLS else 1,
                                       interval_ms=ms, vol=sr.VOL[tf], start_ts=1_700_000_000_000 // 86_400_000 * 86_400_000)
            else:
                path, err = ensure_data(sym, tf, a.years, a.data, a.refresh)
                if err:
                    notes.append(f"{sym} {tf}: dilewati, {err}")
                    print(f"[lewati] {sym} {tf}: {err}")
                    continue
                c = load_csv(path)
            d0 = dt.datetime.fromtimestamp(c[0].ts / 1000, dt.timezone.utc)
            d1 = dt.datetime.fromtimestamp(c[-1].ts / 1000, dt.timezone.utc)
            bh = (c[-1].close / c[0].close - 1) * 100
            L += [f"=== {sym} {tf} ===", f"Data: {len(c)} candle, {d0:%Y-%m-%d} s/d {d1:%Y-%m-%d}; beli-dan-tahan {bh:+.1f}%"]
            for market in ("spot", "futures"):
                print(f"menjalankan {sym} {tf} {market} ...", flush=True)
                r = sr.run(market, tf, sym, c, a.funding_8h)
                v, ok = verdict(r, tf)
                L += [fmt_row(market.upper(), r), f"          strategi {STRAT[(market, tf)]} -> {v}"]
                results += 1
                if ok:
                    passed.append(f"{sym} {tf} {market.upper()}")
            L.append("")
    L.append("=== RINGKASAN ===")
    L.append("Lolos saringan awal: " + (", ".join(passed) if passed else "TIDAK ADA"))
    L += notes
    L += ["", "Catatan: saringan awal = transaksi >=30 (4h) / >=15 (1d), return>0, PF>=1.2, maxDD<=15%, walk-forward >=3/4.",
          "Ini bukan bukti profit dan bukan gerbang G0-G8. LIVE tetap terkunci. Funding/likuidasi belum dimodelkan di mesin."]
    if results == 0:
        L.insert(5, "TIDAK ADA hasil: semua data gagal diunduh. Periksa internet / blokir Binance (HTTP 451).")
    text = "\n".join(L) + "\n"
    out = os.path.join(ROOT, "docs", "swing")
    os.makedirs(out, exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M")
    prefix = "SINTETIS_" if a.synthetic else ""
    p_latest = os.path.join(out, f"{prefix}REPORT_terbaru.txt")
    p_dated = os.path.join(out, f"{prefix}REPORT_{stamp}.txt")
    for p in (p_latest, p_dated):
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    print("\n" + text)
    print("Laporan:", p_latest)
    dl = os.path.expanduser("~/storage/downloads")
    if os.path.isdir(dl):
        dst = os.path.join(dl, f"olypahantstrade_{prefix}laporan_{stamp}.txt")
        shutil.copyfile(p_latest, dst)
        print("Salinan di folder Download HP:", dst)


if __name__ == "__main__":
    main()
