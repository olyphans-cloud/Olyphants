"""Registri gerbang rilis G0–G8 (Master Roadmap v1.0 bagian 14) dan kunci LIVE."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Mapping

from .config import LIVE_CONFIRM_PHRASE, Config, ConfigError, config_hash

GATE_NAMES = {
    "G0": "Specification",
    "G1": "Data",
    "G2": "Research (backtest anti-leakage + cost model + WFO/OOS)",
    "G3": "System (integration/E2E/failure/recovery)",
    "G4": "Shadow (pipeline live tanpa order)",
    "G5": "Paper (data nyata + order virtual)",
    "G6": "Testnet (order/fill/recovery nyata di testnet)",
    "G7": "Canary (live modal minimal)",
    "G8": "Controlled Live (ekspansi)",
}
GATE_IDS = list(GATE_NAMES)
# G7 adalah fase live minimal itu sendiri: untuk MEMBUKA live (canary) semua G0–G6 harus lulus.
LIVE_PREREQUISITE_UPTO = "G6"


def load_gates(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"gates": {g: {"passed": False, "evidence": None} for g in GATE_IDS}}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("gates", {})
    for g in GATE_IDS:
        data["gates"].setdefault(g, {"passed": False, "evidence": None})
    return data


def missing_gates(data: Mapping[str, Any], upto: str = LIVE_PREREQUISITE_UPTO) -> List[str]:
    idx = GATE_IDS.index(upto)
    out = []
    for g in GATE_IDS[: idx + 1]:
        entry = data["gates"].get(g, {})
        if not (entry.get("passed") is True and entry.get("evidence")):
            out.append(g)
    return out


def require_live_unlock(cfg: Config, env: Mapping[str, str]) -> None:
    """Melempar ConfigError kecuali SEMUA syarat terpenuhi. Tidak pernah mengubah berkas."""
    if env.get("BTC_ROBOT_LIVE_CONFIRM") != LIVE_CONFIRM_PHRASE:
        raise ConfigError("LIVE terkunci: env BTC_ROBOT_LIVE_CONFIRM tidak sesuai")
    data = load_gates(cfg.gates_file)
    miss = missing_gates(data, LIVE_PREREQUISITE_UPTO)
    if miss:
        raise ConfigError(f"LIVE terkunci: gerbang belum lulus/tanpa bukti: {miss}")
    approval = data.get("owner_approval") or {}
    if approval.get("config_hash") != config_hash(cfg) or not approval.get("approved_by"):
        raise ConfigError("LIVE terkunci: persetujuan pemilik tidak ada atau tidak cocok dengan hash config")


def status_report(path: str) -> str:
    data = load_gates(path)
    lines = []
    for g in GATE_IDS:
        e = data["gates"][g]
        mark = "LULUS" if (e.get("passed") is True and e.get("evidence")) else "BELUM"
        lines.append(f"{g} {GATE_NAMES[g]}: {mark}  bukti={e.get('evidence')}")
    return "\n".join(lines)
