#!/usr/bin/env python3
"""Unduh klines BTCUSDT dari Binance (publik, tanpa API key) ke CSV.

Format keluaran sesuai robot: ts,open,high,low,close,volume (ts = ms UTC).
Hanya library standar Python. Butuh internet.

Pakai:
  python3 download_btc_history.py                    # 3 tahun, 15m
  python3 download_btc_history.py --years 3 --interval 15m --out BTCUSDT_15m.csv
"""
import argparse
import csv
import json
import time
import urllib.error
import urllib.request

HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api.binance.us",
]
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
               "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def fetch(host, symbol, interval, start_ms, end_ms):
    url = (f"{host}/api/v3/klines?symbol={symbol}&interval={interval}"
           f"&startTime={start_ms}&endTime={end_ms}&limit=1000")
    req = urllib.request.Request(url, headers={"User-Agent": "btc-robot-dl"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def fetch_retry(hosts, *args):
    last = None
    for attempt in range(4):
        for host in hosts:
            try:
                return host, fetch(host, *args)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = e
        time.sleep(2 * (attempt + 1))
    raise SystemExit(f"Gagal mengunduh setelah beberapa percobaan: {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="15m", choices=INTERVAL_MS)
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--out", default="BTCUSDT_15m.csv")
    a = ap.parse_args()

    step = INTERVAL_MS[a.interval]
    now = int(time.time() * 1000)
    end_closed = (now // step) * step - 1          # hanya candle yang sudah tutup
    start = ((now - int(a.years * 365 * 86_400_000)) // step) * step

    rows, cursor, hosts = [], start, HOSTS
    print(f"Unduh {a.symbol} {a.interval} dari ts={start} ...")
    while cursor < end_closed:
        host, data = fetch_retry(hosts, a.symbol, a.interval, cursor, end_closed)
        hosts = [host] + [h for h in HOSTS if h != host]   # pakai host yang berhasil
        if not data:
            break
        for k in data:
            if int(k[0]) + step - 1 <= end_closed:
                rows.append((int(k[0]), k[1], k[2], k[3], k[4], k[5]))
        cursor = int(data[-1][0]) + step
        print(f"  {len(rows)} candle ...", end="\r")
        time.sleep(0.15)

    # buang duplikat, urutkan
    uniq = sorted({r[0]: r for r in rows}.values())
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        w.writerows(uniq)
    print(f"\nSelesai: {len(uniq)} candle -> {a.out}")
    print(f"Lanjut: python3 -m btc_robot --config config.mtf.example.json validate-data --csv {a.out}")


if __name__ == "__main__":
    main()
