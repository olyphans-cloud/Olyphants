"""Ingestion + validasi data (Dok 01 bagian 3 A/B, gerbang G1)."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .models import BookQuote, Candle


class DataError(RuntimeError):
    pass


@dataclass
class DataQualityReport:
    ok: bool
    n: int
    issues: List[str] = field(default_factory=list)
    last_ts: Optional[int] = None
    age_seconds: Optional[float] = None

    def summary(self) -> str:
        return "OK" if self.ok else "; ".join(self.issues[:10])


def validate_candles(
    candles: Sequence[Candle],
    interval_ms: int,
    now_ms: Optional[int] = None,
    max_age_seconds: Optional[float] = None,
    allow_gaps: bool = False,
) -> DataQualityReport:
    issues: List[str] = []
    n = len(candles)
    if n == 0:
        return DataQualityReport(False, 0, ["data kosong"])
    prev: Optional[Candle] = None
    for i, c in enumerate(candles):
        vals = (c.open, c.high, c.low, c.close, c.volume)
        if any((not isinstance(v, (int, float))) or math.isnan(v) or math.isinf(v) for v in vals):
            issues.append(f"idx {i}: nilai non-finite")
            prev = c
            continue
        if min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0:
            issues.append(f"idx {i}: harga<=0 atau volume<0")
        if c.high < max(c.open, c.close) or c.low > min(c.open, c.close) or c.high < c.low:
            issues.append(f"idx {i}: OHLC tidak konsisten")
        if c.ts % interval_ms != 0:
            issues.append(f"idx {i}: ts tidak selaras interval")
        if prev is not None:
            if c.ts == prev.ts:
                issues.append(f"idx {i}: ts duplikat {c.ts}")
            elif c.ts < prev.ts:
                issues.append(f"idx {i}: ts mundur {c.ts} < {prev.ts}")
            elif c.ts - prev.ts != interval_ms and not allow_gaps:
                missing = (c.ts - prev.ts) // interval_ms - 1
                issues.append(f"idx {i}: celah {missing} candle sebelum ts {c.ts}")
        prev = c
    last = candles[-1]
    age = None
    if now_ms is not None:
        # umur dihitung dari waktu TUTUP candle terakhir
        age = (now_ms - (last.ts + interval_ms)) / 1000.0
        if age < -1:
            issues.append("candle terakhir belum tutup / ts di masa depan")
        if max_age_seconds is not None and age > max_age_seconds:
            issues.append(f"data basi: umur {age:.0f}s > {max_age_seconds}s")
    return DataQualityReport(not issues, n, issues, last.ts, age)


def data_fingerprint(candles: Sequence[Candle]) -> str:
    h = hashlib.sha256()
    for c in candles:
        h.update(f"{c.ts},{c.open!r},{c.high!r},{c.low!r},{c.close!r},{c.volume!r};".encode())
    return h.hexdigest()


def load_csv(path: str) -> List[Candle]:
    """CSV dengan kolom ts,open,high,low,close,volume (ts epoch ms UTC)."""
    out: List[Candle] = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        need = {"ts", "open", "high", "low", "close", "volume"}
        if not rd.fieldnames or not need.issubset(set(rd.fieldnames)):
            raise DataError(f"CSV harus punya kolom {sorted(need)}")
        for row in rd:
            try:
                out.append(Candle(int(row["ts"]), float(row["open"]), float(row["high"]),
                                  float(row["low"]), float(row["close"]), float(row["volume"])))
            except (ValueError, TypeError) as exc:
                raise DataError(f"baris CSV rusak: {row!r}") from exc
    return out


def generate_synthetic(n: int, seed: int, interval_ms: int = 3_600_000,
                       start_ts: int = 1_700_000_000_000 // 3_600_000 * 3_600_000,
                       start_price: float = 30_000.0, vol: float = 0.008) -> List[Candle]:
    """Data SINTETIS (random walk bertrend/regime) hanya untuk tes mesin. Bukan data pasar.

    vol = simpangan baku return per candle (default 0.8% ~ candle 1h; pakai ~0.0007 untuk candle 1m).
    """
    rng = random.Random(seed)
    k = vol / 0.008  # k == 1.0 persis pada default -> hasil lama tidak berubah
    price = start_price
    drift = 0.0
    out: List[Candle] = []
    for i in range(n):
        if i % 200 == 0:
            drift = rng.uniform(-0.0008, 0.0012) * k
        ret = drift + rng.gauss(0, vol)
        o = price
        c = max(1.0, o * (1 + ret))
        hi = max(o, c) * (1 + abs(rng.gauss(0, 0.002 * k)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.002 * k)))
        out.append(Candle(start_ts + i * interval_ms, o, hi, lo, c, rng.uniform(50, 500)))
        price = c
    return out


def fetch_binance_klines(symbol: str, interval: str, limit: int = 500,
                         base_url: str = "https://api.binance.com",
                         retries: int = 3, timeout: float = 10.0) -> List[Candle]:
    """Endpoint publik (tanpa API key). BELUM diuji terhadap jaringan sungguhan di sesi pembuatan."""
    qs = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    url = f"{base_url}/api/v3/klines?{qs}"
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                rows = json.loads(resp.read().decode())
            if not isinstance(rows, list):
                raise ValueError("respons klines bukan list (mungkin objek error API)")
            return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                    for r in rows]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            last_exc = exc
            time.sleep(min(2 ** attempt, 8))
    raise DataError(f"gagal mengambil klines setelah {retries} percobaan: {last_exc}")


def closed_only(candles: Sequence[Candle], interval_ms: int, now_ms: int) -> List[Candle]:
    """Buang candle yang belum tutup."""
    return [c for c in candles if c.ts + interval_ms <= now_ms]


def fetch_book_ticker(symbol: str, base_url: str = "https://api.binance.com",
                      timeout: float = 5.0) -> BookQuote:
    """Bid/ask teratas dari endpoint publik bookTicker (tanpa API key).

    BELUM diuji terhadap Binance sungguhan di sesi pembuatan; diuji terhadap server tiruan lokal.
    Kegagalan apa pun -> DataError (pemanggil wajib memperlakukannya sebagai 'spread tidak diketahui').
    """
    url = f"{base_url}/api/v3/ticker/bookTicker?{urllib.parse.urlencode({'symbol': symbol})}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            row = json.loads(resp.read().decode())
        bid, ask = float(row["bidPrice"]), float(row["askPrice"])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError, OSError) as exc:
        raise DataError(f"gagal mengambil bookTicker: {exc}") from exc
    t = time.time()
    return BookQuote(bid, ask, observed_at=t, now=t)


def fetch_tokocrypto_klines(
    symbol: str, interval: str, limit: int = 500,
    base_url: str = "https://www.tokocrypto.site",
    retries: int = 3, timeout: float = 10.0,
) -> List[Candle]:
    """Candles publik Tokocrypto. Tidak menggunakan Binance."""
    qs = urllib.parse.urlencode({
        "symbol": symbol, "interval": interval, "limit": limit
    })
    url = f"{base_url}/api/v3/klines?{qs}"
    last_exc: Optional[Exception] = None

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                rows = json.loads(resp.read().decode())
            if not isinstance(rows, list):
                raise ValueError("respons Tokocrypto klines bukan list")
            out = [
                Candle(int(r[0]), float(r[1]), float(r[2]),
                      float(r[3]), float(r[4]), float(r[5]))
                for r in rows
            ]
            if not out:
                raise ValueError("Tokocrypto mengembalikan candle kosong")
            return out
        except (
            urllib.error.URLError, TimeoutError, OSError,
            ValueError, KeyError, IndexError, TypeError
        ) as exc:
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 8))

    raise DataError(
        f"Tokocrypto klines gagal setelah {retries} percobaan: {last_exc}"
    )


def fetch_tokocrypto_book_ticker(
    symbol: str,
    base_url: str = "https://www.tokocrypto.site",
    timeout: float = 5.0,
) -> BookQuote:
    """Bid/ask dari order book Tokocrypto. Gagal -> spread tidak diketahui."""
    qs = urllib.parse.urlencode({"symbol": symbol, "limit": 5})
    url = f"{base_url}/api/v3/depth?{qs}"

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            row = json.loads(resp.read().decode())

        bids, asks = row["bids"], row["asks"]
        if not bids or not asks:
            raise ValueError("order book kosong")

        bid, ask = float(bids[0][0]), float(asks[0][0])
        if not (math.isfinite(bid) and math.isfinite(ask)
                and bid > 0 and ask >= bid):
            raise ValueError("bid/ask tidak valid")

    except (
        urllib.error.URLError, TimeoutError, OSError,
        ValueError, KeyError, TypeError, IndexError
    ) as exc:
        raise DataError(f"Tokocrypto order book gagal: {exc}") from exc

    t = time.time()
    return BookQuote(bid, ask, observed_at=t, now=t)
