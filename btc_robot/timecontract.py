"""Kontrak Data & Waktu (Master Roadmap bagian 2).

Prinsip: UNKNOWN != NEUTRAL; data terlambat tidak boleh merekonstruksi keputusan sebelum diterima;
kegagalan jam = tidak ada trade baru; setiap keputusan memakai snapshot immutable ber-ID.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional, Sequence, Tuple


class DataStatus(str, Enum):
    KNOWN = "KNOWN"
    STALE = "STALE"
    MISSING = "MISSING"
    CONFLICTING = "CONFLICTING"
    UNKNOWN = "UNKNOWN"


class Finality(str, Enum):
    PROVISIONAL = "PROVISIONAL"
    FINAL = "FINAL"
    CORRECTED = "CORRECTED"


@dataclass(frozen=True)
class Stamped:
    name: str
    value: Any
    status: DataStatus
    source: str
    event_time: Optional[int]      # kapan kejadian (ms UTC)
    receive_time: Optional[int]    # kapan robot menerima
    quality: Optional[float] = None
    version: str = "1"
    finality: Finality = Finality.FINAL

    @property
    def latency_ms(self) -> Optional[int]:
        if self.event_time is None or self.receive_time is None:
            return None
        return self.receive_time - self.event_time


def stamp(name: str, value: Any, source: str, event_time: Optional[int], receive_time: Optional[int],
          now_ms: int, max_age_ms: int, *, conflicting: bool = False, quality: Optional[float] = None,
          version: str = "1", finality: Finality = Finality.FINAL,
          skew_tolerance_ms: int = 1_000) -> Stamped:
    """Menetapkan status data. Tidak pernah mengembalikan KNOWN bila ada keraguan."""
    def mk(status: DataStatus) -> Stamped:
        return Stamped(name, value, status, source, event_time, receive_time, quality, version, finality)

    if value is None:
        return mk(DataStatus.MISSING)
    if event_time is None or receive_time is None:
        return mk(DataStatus.UNKNOWN)
    if receive_time > now_ms + skew_tolerance_ms or event_time > receive_time + skew_tolerance_ms:
        return mk(DataStatus.UNKNOWN)          # timestamp tidak konsisten (masa depan / diterima sebelum terjadi)
    if conflicting:
        return mk(DataStatus.CONFLICTING)
    if now_ms - event_time > max_age_ms:
        return mk(DataStatus.STALE)
    return mk(DataStatus.KNOWN)


def usable_for_decision(s: Stamped, decision_time: int, allow_provisional: bool = False) -> Tuple[bool, str]:
    if s.status != DataStatus.KNOWN:
        return False, f"{s.name}:{s.status.value}"       # UNKNOWN/STALE/... tidak pernah dianggap netral
    if s.receive_time is None or s.receive_time > decision_time:
        return False, f"{s.name}:diterima_setelah_keputusan"  # anti look-ahead / late-arriving
    if s.finality == Finality.PROVISIONAL and not allow_provisional:
        return False, f"{s.name}:provisional"
    return True, "ok"


@dataclass(frozen=True)
class ClockHealth:
    max_skew_ms: int = 1_000

    def check(self, local_ms: Optional[int], reference_ms: Optional[int]) -> Tuple[bool, str]:
        """Jam adalah dependensi keras: referensi hilang = TIDAK sehat."""
        if local_ms is None or reference_ms is None:
            return False, "referensi_waktu_tidak_tersedia"
        skew = abs(local_ms - reference_ms)
        if skew > self.max_skew_ms:
            return False, f"skew_{skew}ms_melebihi_{self.max_skew_ms}ms"
        return True, "ok"


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class DecisionSnapshot:
    decision_id: str
    decision_time: int
    inputs: Tuple[Stamped, ...]
    versions: Tuple[Tuple[str, str], ...]   # (komponen, versi): strategy, model, prompt, config, risk, schema

    @staticmethod
    def build(decision_time: int, inputs: Sequence[Stamped], versions: Sequence[Tuple[str, str]]) -> "DecisionSnapshot":
        ins = tuple(inputs)
        vers = tuple(sorted((str(k), str(v)) for k, v in versions))
        body = {"t": decision_time, "inputs": [i.__dict__ for i in ins], "versions": vers}
        did = hashlib.sha256(_canon(body).encode()).hexdigest()[:20]
        return DecisionSnapshot(did, decision_time, ins, vers)

    def unusable(self, allow_provisional: bool = False) -> List[str]:
        out = []
        for s in self.inputs:
            ok, why = usable_for_decision(s, self.decision_time, allow_provisional)
            if not ok:
                out.append(why)
        return out


def make_decision_id(strategy_version: str, bar_ts: int, data_fingerprint: str, config_hash: str) -> str:
    """ID keputusan deterministik: replay dengan data+config+versi yang sama menghasilkan ID yang sama."""
    raw = f"{strategy_version}|{bar_ts}|{data_fingerprint}|{config_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]
