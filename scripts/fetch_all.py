#!/usr/bin/env python3
"""Unduh BTC/BNB/SOL (15m, 1h, 4h, 1d) ke folder data/. Butuh internet."""
import argparse, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--years", default="3", help="tahun untuk 15m dan 1h")
ap.add_argument("--years-long", default="5", help="tahun untuk 4h dan 1d")
ap.add_argument("--out", default="data")
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
for sym in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"):
    for tf in ("15m", "1h", "4h", "1d"):
        out = os.path.join(a.out, f"{sym}_{tf}.csv")
        if os.path.exists(out) and os.path.getsize(out) > 1000:
            print("== sudah ada, dilewati:", out)
            continue
        print("==", out)
        yrs = a.years if tf in ("15m", "1h") else a.years_long
        subprocess.run([sys.executable, os.path.join(HERE, "download_btc_history.py"), "--symbol", sym,
                        "--interval", tf, "--years", yrs, "--out", out], check=True)
