"""Audit log JSONL dengan hash-chain (tamper-evident) dan redaksi rahasia (Dok 01 bagian 7)."""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional

from .config import CODE_VERSION

_SECRET_KEY = re.compile(r"(api[_-]?key|secret|private|seed|mnemonic|passphrase|password|token|auth)", re.I)
GENESIS = "0" * 64
# Token bot Telegram (<angka>:<35+ karakter>) dan segmen URL 'bot<token>/' dapat muncul di TEKS pesan error.
_TOKEN_IN_TEXT = re.compile(r"(?:bot)?\d{6,12}:[A-Za-z0-9_-]{30,}")


def redact(obj: Any) -> Any:
    """Redaksi berdasarkan NAMA kunci, ditambah pola token Telegram di dalam teks (nilai lain tidak disentuh)."""
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if _SECRET_KEY.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _TOKEN_IN_TEXT.sub("[REDACTED]", obj)
    return obj


def _digest(prev: str, body: Dict[str, Any]) -> str:
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + blob).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Optional[str] = None, config_hash: str = ""):
        self.path = path
        self.config_hash = config_hash
        self.entries: List[Dict[str, Any]] = []
        self.prev = GENESIS
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            self.prev = json.loads(line)["hash"]

    def log(self, event: str, ts: Optional[int] = None, **payload: Any) -> Dict[str, Any]:
        body = {"event": event, "ts": ts, "code_version": CODE_VERSION,
                "config_hash": self.config_hash, "payload": redact(payload)}
        body["prev"] = self.prev
        body["hash"] = _digest(self.prev, {k: v for k, v in body.items() if k != "hash"})
        self.prev = body["hash"]
        self.entries.append(body)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(body, sort_keys=True, default=str) + "\n")
        return body


def verify_chain(entries: List[Dict[str, Any]]) -> bool:
    prev = GENESIS
    for e in entries:
        if e.get("prev") != prev:
            return False
        if _digest(prev, {k: v for k, v in e.items() if k != "hash"}) != e.get("hash"):
            return False
        prev = e["hash"]
    return True


def verify_file(path: str) -> bool:
    with open(path, "r", encoding="utf-8") as fh:
        return verify_chain([json.loads(line) for line in fh if line.strip()])
