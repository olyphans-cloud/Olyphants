"""Guided, fail-closed recovery for the persistent paper-trading halt latch.

This command only changes PAPER state. It never enables testnet/live trading and
never resets equity, positions, ledger history, or risk limits.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

from .audit import AuditLog, verify_file
from .config import Config
from .identity import check_identity
from .safeguard import SafeguardExecutionEngine


def _state_path(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, "paper_state.json")


def _safeguard_path(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, "safeguard_state.json")


def diagnose(cfg: Config) -> Dict[str, Any]:
    path = _state_path(cfg)
    result: Dict[str, Any] = {
        "mode_scope": "PAPER_ONLY",
        "state_file": path,
        "state_exists": os.path.isfile(path),
        "kill_switch_active": os.path.exists(cfg.kill_switch_file) or bool(cfg.shared_kill_switch_file and os.path.exists(cfg.shared_kill_switch_file)),
        "halted": None,
        "halt_reason": None,
        "halt_sources": [],
        "safeguard_halted": False,
        "safeguard_halt_reason": None,
        "audit_chain_ok": None,
        "identity_ok": True,
        "identity_status": "ok",
        "recovery_allowed": False,
        "checks": [],
    }
    id_ok, id_status, id_msg = check_identity(cfg)
    result["identity_ok"], result["identity_status"] = id_ok, id_status
    if not id_ok:
        result["checks"].append(id_msg)
        return result  # identitas tidak cocok: jangan baca/ubah apa pun di direktori ini
    if result["kill_switch_active"]:
        result["checks"].append("KILL_SWITCH masih ada; jangan pulihkan")
    if not result["state_exists"]:
        result["checks"].append("state paper belum ada; jalankan paper-step dan periksa status")
        return result
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            raise ValueError("state root bukan object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result["checks"].append(f"state tidak bisa dibaca: {type(exc).__name__}")
        return result
    risk_halted = bool(state.get("halted", False))
    result["halt_reason"] = state.get("halt_reason")
    # Latch safeguard disimpan di berkas terpisah; harus ikut dilihat (jika tidak, halt tak bisa dipulihkan).
    sg = SafeguardExecutionEngine(state_path=_safeguard_path(cfg))  # berkas rusak => halted (fail-closed)
    result["safeguard_halted"], result["safeguard_halt_reason"] = sg.halted, sg.halt_reason
    result["halt_sources"] = (["risk"] if risk_halted else []) + (["safeguard"] if sg.halted else [])
    result["halted"] = risk_halted or sg.halted
    audit_path = os.path.join(cfg.state_dir, "paper_audit.jsonl")
    if os.path.exists(audit_path):
        try:
            result["audit_chain_ok"] = verify_file(audit_path)
        except Exception:
            result["audit_chain_ok"] = False
    else:
        result["audit_chain_ok"] = True  # belum ada log untuk diverifikasi
    if not result["halted"]:
        result["checks"].append("robot tidak sedang halt; tidak ada yang perlu dipulihkan")
    if result["audit_chain_ok"] is not True:
        result["checks"].append("audit hash-chain gagal; pemulihan ditolak")
    if result["halted"] and not result["kill_switch_active"] and result["audit_chain_ok"] is True:
        result["recovery_allowed"] = True
        result["checks"].append("prasyarat teknis awal lolos; operator tetap harus memastikan akar masalah selesai")
    return result


def recover(cfg: Config, approved_by: str, reason: str, confirm_resolved: bool) -> Dict[str, Any]:
    """Clear only the paper halt latch after explicit human confirmation and checks."""
    approved_by = (approved_by or "").strip()
    reason = (reason or "").strip()
    if not approved_by:
        raise ValueError("approved_by wajib diisi")
    if not reason:
        raise ValueError("reason wajib menjelaskan perbaikan")
    if not confirm_resolved:
        raise ValueError("wajib --confirm-resolved setelah akar masalah diperiksa")
    report = diagnose(cfg)
    if not report["identity_ok"]:
        raise ValueError(f"identitas state tidak cocok ({report['identity_status']}); pemulihan ditolak")
    if not report["state_exists"]:
        raise ValueError("state paper tidak ditemukan; tidak ada state yang aman untuk dipulihkan")
    if report["kill_switch_active"]:
        raise ValueError("KILL_SWITCH masih aktif; selesaikan pemicunya terlebih dahulu")
    if report["audit_chain_ok"] is not True:
        raise ValueError("audit hash-chain tidak valid; halt tetap terkunci")
    if report["halted"] is not True:
        raise ValueError("state tidak sedang halted; pemulihan ditolak")

    path = _state_path(cfg)
    with open(path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    old_reason = state.get("halt_reason")
    old_sg_reason = report["safeguard_halt_reason"]
    state["halted"] = False
    state["halt_reason"] = None
    ledger_pos = (state.get("ledger") or {}).get("position")
    if state.get("machine_state") == "RECONCILING" and not ledger_pos:
        state["machine_state"] = "READY"   # rekonsiliasi dinyatakan selesai oleh operator (ledger datar)
    state["recovery"] = {"approved_by": approved_by, "reason": reason,
                         "previous_halt_reason": old_reason, "previous_safeguard_halt_reason": old_sg_reason}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    if report["safeguard_halted"]:
        SafeguardExecutionEngine(state_path=_safeguard_path(cfg)).release_halt()  # loss_streak/cooldown tetap
    AuditLog(os.path.join(cfg.state_dir, "paper_audit.jsonl")).log(
        "paper_halt_recovered", None, approved_by=approved_by, reason=reason,
        previous_halt_reason=old_reason, previous_safeguard_halt_reason=old_sg_reason,
        halt_sources=report["halt_sources"], mode="PAPER")
    return {"status": "recovered", "mode_scope": "PAPER_ONLY", "halted": False,
            "approved_by": approved_by, "reason": reason,
            "previous_halt_reason": old_reason, "previous_safeguard_halt_reason": old_sg_reason,
            "halt_sources": report["halt_sources"],
            "note": "Saldo, ledger, posisi, dan riwayat tidak di-reset. Periksa status dan jalankan paper-step secara terpantau."}
