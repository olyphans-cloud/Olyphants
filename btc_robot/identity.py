"""Identitas state per sleeve (mode x simbol x interval): mencegah M1/H1/MTF saling memuat atau menimpa state.

Setiap state_dir memuat identity.json yang ditulis saat inisialisasi. Ketidakcocokan, berkas identitas rusak, atau
state lama TANPA identitas => DITOLAK (tanpa menulis apa pun ke direktori itu). Adopsi state lama hanya lewat
perintah eksplisit adopt-state --confirm-identity.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from .config import Config

IDENTITY_FILE = "identity.json"
_STATE_FILES = ("paper_state.json", "safeguard_state.json", "paper_audit.jsonl")


def identity_of(cfg: Config) -> Dict[str, str]:
    return {"strategy_mode": cfg.strategy_mode, "symbol": cfg.symbol, "interval": cfg.interval}


def _path(state_dir: str) -> str:
    return os.path.join(state_dir, IDENTITY_FILE)


def check_identity(cfg: Config) -> Tuple[bool, str, str]:
    """(ok, status, pesan). status: ok | fresh | state_identity_mismatch | state_identity_missing | state_identity_corrupt."""
    d = cfg.state_dir
    p = _path(d)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if not isinstance(stored, dict):
                raise ValueError("bukan object")
        except (OSError, ValueError):
            return False, "state_identity_corrupt", f"{p} tidak terbaca; periksa manual (state tidak disentuh)"
        want = identity_of(cfg)
        got = {k: stored.get(k) for k in want}
        if got != want:
            return (False, "state_identity_mismatch",
                    f"state_dir '{d}' milik {got}, bukan {want}. Gunakan state_dir terpisah per mode; tidak ada yang ditulis.")
        return True, "ok", ""
    if any(os.path.exists(os.path.join(d, f)) for f in _STATE_FILES):
        return (False, "state_identity_missing",
                f"state lama di '{d}' tidak punya identitas; jalankan adopt-state --confirm-identity "
                "hanya jika Anda yakin state itu milik konfigurasi ini")
    return True, "fresh", ""


def write_identity(cfg: Config, adopted: bool = False) -> None:
    os.makedirs(cfg.state_dir, exist_ok=True)
    payload: Dict[str, Any] = dict(identity_of(cfg))
    if adopted:
        payload["adopted"] = True
    tmp = _path(cfg.state_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
    os.replace(tmp, _path(cfg.state_dir))


def adopt(cfg: Config, confirmed: bool) -> str:
    if not confirmed:
        raise ValueError("adopsi state lama butuh --confirm-identity")
    ok, status, msg = check_identity(cfg)
    if status == "ok":
        return "identitas sudah ada dan cocok"
    if status == "state_identity_mismatch" or status == "state_identity_corrupt":
        raise ValueError(msg)
    write_identity(cfg, adopted=True)
    return "identitas ditulis (adopted)"
