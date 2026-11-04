#!/usr/bin/env python3
"""Satu perintah: unduh data (jika belum ada) -> backtest BTC/BNB/SOL di 4h dan 1d (spot dan futures)
-> tulis laporan .txt yang enak dibaca di HP.  Hanya library standar Python.

  python3 scripts/laporan_txt.py                 # data nyata (butuh internet untuk unduhan pertama)
  python3 scripts/laporan_txt.py --synthetic     # uji offline (data palsu, hanya cek mesin)
Keluaran: laporan/laporan_YYYYMMDD_HHMM.txt, laporan/laporan_terbaru.txt, dan salinan ke
~/storage/downloads/ bila ada (Termux setelah termux-setup-storage).
"""
import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from btc_robot.data import generate_synthetic, load_csv      # noqa: E402
import swing_report as sw                                     # noqa: E402

SYMBOLS = ["BTCUSDT", "BNBUSDT", "SOLUSDT"]
TFS = ["4h", "1d"]
MIN_TRADES, MIN_PF, MAX_DD, MIN_WF = 30, 1.2, 15.0, 3


def download(sym, tf, years, out):
    if os.path.exists(out) and os.path.getsize(out) > 1000:
        return None
    os.makedirs(os.path.dirname(out), exist_ok=True)
    r = subprocess.run([sys.executable, os.path.join(HERE, "download_btc_history.py"), "--symbol", sym,
                        "--interval", tf, "--years", str(years), "--out", out],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        return (r.stderr or r.stdout or "gagal").strip().splitlines()[-1][:160]
    return None


def verdict(r):
    if r["n"] < MIN_TRADES:
        return f"DATA KURANG (trx {r['n']} < {MIN_TRADES})"
    pf = r["pf"] if r["pf"] is not None else 0.0
    wf = r["wf"] if isinstance(r["wf"], int) else 0
    bad = []
    if r["ret"] <= 0:
        bad.append("return<=0")
    if pf < MIN_PF:
        bad.append(f"PF<{MIN_PF}")
    if r["dd"] > MAX_DD:
        bad.append(f"DD>{MAX_DD:.0f}%")
    if wf < MIN_WF:
        bad.append(f"WF<{MIN_WF}/4")
    return "LOLOS SARING" if not bad else "TIDAK LOLOS: " + ", ".join(bad)


def fmt_date(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "data"))
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--funding-8h", type=float, default=0.0001)
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "laporan"))
    a = ap.parse_args()

    now = dt.datetime.now()
    L = ["=" * 44, "LAPORAN ROBOT olypahantstrade (swing 4h/1d)", "=" * 44,
         f"Waktu    : {now:%Y-%m-%d %H:%M}",
         f"Sumber   : {'SINTETIS (data palsu, hanya uji mesin)' if a.synthetic else 'data nyata Binance'}",
         "Simbol   : " + ", ".join(SYMBOLS),
         "Strategi : 4h = d1h4 (spot long / futures long+short); 1d = lab_trend",
         f"Funding futures dipotong {a.funding_8h*100:.3f}%/8 jam (estimasi pasca-hoc)",
         f"Saringan : trx>={MIN_TRADES}, PF>={MIN_PF}, DD<={MAX_DD:.0f}%, WF>={MIN_WF}/4, return>0", ""]
    passed, notes = [], []
    for sym in SYMBOLS:
        for tf in TFS:
            L.append("-" * 44)
            L.append(f"{sym} {tf}")
            if a.synthetic:
                c = generate_synthetic(sw.N[tf], seed=SYMBOLS.index(sym) + 1, interval_ms=sw.MS[tf], vol=sw.VOL[tf],
                                       start_ts=1_700_000_000_000 // sw.MS[tf] * sw.MS[tf])
            else:
                path = os.path.join(a.data, f"{sym}_{tf}.csv")
                if not a.no_download:
                    err = download(sym, tf, a.years, path)
                    if err:
                        L.append(f"  GAGAL UNDUH: {err}")
                        notes.append(f"{sym} {tf}: gagal unduh")
                        continue
                if not os.path.exists(path):
                    L.append("  file data tidak ada, dilewati")
                    notes.append(f"{sym} {tf}: tidak ada data")
                    continue
                c = load_csv(path)
            L.append(f"  data: {len(c)} candle, {fmt_date(c[0].ts)} s/d {fmt_date(c[-1].ts)}")
            for market in ("spot", "futures"):
                try:
                    r = sw.run(market, tf, sym, c, a.funding_8h)
                except Exception as e:      # satu kombinasi gagal tidak menghentikan laporan
                    L.append(f"  {market.upper():8}: ERROR {type(e).__name__}: {e}")
                    notes.append(f"{sym} {tf} {market}: error")
                    continue
                pf = "-" if r["pf"] is None else f"{r['pf']:.2f}"
                v = verdict(r)
                L.append(f"  {market.upper():8}: trx {r['n']}, return {r['ret']:+.1f}%, DD {r['dd']:.1f}%, PF {pf}, WF {r['wf']}/4")
                if market == "futures":
                    L.append(f"            long {r['LONG'][0]} trx {r['LONG'][1]:+.1f}% | short {r['SHORT'][0]} trx {r['SHORT'][1]:+.1f}% | funding -{r['fund']:.2f}%")
                L.append(f"            => {v}")
                if v == "LOLOS SARING":
                    passed.append(f"{sym} {tf} {market}")
    L += ["", "=" * 44, "RINGKASAN", "=" * 44]
    L.append("Lolos saringan: " + (", ".join(passed) if passed else "TIDAK ADA"))
    if notes:
        L.append("Catatan: " + "; ".join(notes))
    L += ["", "PENTING:",
          "- Lolos saringan BUKAN bukti profit. Ini baru backtest satu periode;",
          "  gerbang G0-G8 robot belum lulus dan mode LIVE tetap terkunci.",
          "- Jika sumber SINTETIS, angka tidak bermakna untuk pasar nyata.",
          "- Backtest belum memodelkan likuidasi futures dan funding asli.",
          "- Langkah berikut jika ada yang lolos: paper trading beberapa minggu."]
    text = "\n".join(L) + "\n"
    os.makedirs(a.out, exist_ok=True)
    stamp = f"{now:%Y%m%d_%H%M}" + ("_sintetis" if a.synthetic else "")
    f1 = os.path.join(a.out, f"laporan_{stamp}.txt")
    with open(f1, "w", encoding="utf-8") as fh:
        fh.write(text)
    shutil.copyfile(f1, os.path.join(a.out, "laporan_terbaru.txt"))
    dl = os.path.expanduser("~/storage/downloads")
    if os.path.isdir(dl):
        shutil.copyfile(f1, os.path.join(dl, os.path.basename(f1)))
        L2 = f"Salinan juga di: {dl}/{os.path.basename(f1)}"
    else:
        L2 = "(folder Download tidak ditemukan; jalankan termux-setup-storage untuk salinan otomatis)"
    print(text)
    print("Laporan tersimpan:", f1)
    print(L2)


if __name__ == "__main__":
    main()
