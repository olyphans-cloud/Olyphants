#!/usr/bin/env python3
"""Uji pengambilan data pasar PUBLIK (tanpa API key, tanpa order) dan validasi kualitasnya. Jalankan di perangkat
yang punya internet:

  python3 scripts/verify_live_data.py                      # BTCUSDT 1m dan 1h
  python3 scripts/verify_live_data.py --symbol BTCUSDT --base-url https://api.binance.com

Keluar kode 0 bila semua pemeriksaan lolos, 1 bila ada yang gagal. Hasil JSON dicetak ke stdout.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btc_robot.data import (DataError, closed_only, fetch_binance_klines, fetch_book_ticker,  # noqa: E402
                            validate_candles)

INTERVALS = {"1m": 60_000, "1h": 3_600_000}


def run(symbol, base_url, now_ms=None, limit=500):
    import time
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    report = {"symbol": symbol, "checks": [], "ok": True}

    def add(name, ok, detail=""):
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        report["ok"] = report["ok"] and bool(ok)
    for itv, ms in INTERVALS.items():
        try:
            raw = fetch_binance_klines(symbol, itv, limit, base_url=base_url, retries=2, timeout=10.0)
        except DataError as exc:
            add(f"{itv}:ambil_klines", False, str(exc)[:200])
            continue
        add(f"{itv}:ambil_klines", len(raw) > 0, f"{len(raw)} candle")
        closed = closed_only(raw, ms, now_ms)
        rep = validate_candles(closed, ms, now_ms=now_ms, max_age_seconds=2 * ms / 1000 + 120)
        add(f"{itv}:kualitas_candle", rep.ok, "; ".join(rep.issues[:3]))
        add(f"{itv}:candle_terakhir_sudah_tutup", bool(closed) and closed[-1].ts + ms <= now_ms)
        add(f"{itv}:jumlah_memadai", len(closed) >= 200, f"{len(closed)} candle tertutup")
    try:
        q = fetch_book_ticker(symbol, base_url=base_url, timeout=10.0)
        spread_pct = (q.ask - q.bid) / q.bid * 100.0
        add("bookTicker", q.ask >= q.bid > 0, f"spread {spread_pct:.4f}%")
    except DataError as exc:
        add("bookTicker", False, str(exc)[:200])
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--base-url", default="https://api.binance.com")
    a = ap.parse_args()
    rep = run(a.symbol, a.base_url)
    print(json.dumps(rep, indent=2))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
