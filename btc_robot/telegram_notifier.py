"""Telegram notifications for the BTC trading robot.

No token is hard-coded here. Credentials are read from environment variables,
or from a local .env file in the current working directory.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _load_local_env(path: str = ".env") -> None:
    """Small .env loader; does not overwrite variables already in the environment.

    Dicari di folder kerja, lalu di folder proyek (agar cron dari folder lain tetap menemukan .env).
    """
    candidates = [Path(path), Path(__file__).resolve().parent.parent / ".env"]
    env_path = next((p for p in candidates if p.is_file()), None)
    if env_path is None:
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class TelegramNotifier:
    """Send notifications only; this class cannot place exchange orders."""

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        enabled: bool | None = None,
        timeout: float = 8.0,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        _load_local_env()
        self.token = token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        if enabled is None:
            enabled = os.getenv("ENABLE_NOTIFICATIONS", "true").strip().lower() in {
                "1", "true", "yes", "on"
            }
        self.enabled = bool(enabled)
        self.timeout = timeout
        self.api_base = api_base.rstrip("/")

    def send_message(self, message: str) -> bool:
        if not self.enabled:
            print("[Telegram] Notifications disabled.")
            return False
        if (not self.token or not self.chat_id or self.token.startswith("PUT_")
                or self.chat_id.startswith("PUT_")):
            print("[Telegram] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID (or still a placeholder).")
            return False

        url = f"{self.api_base}/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": str(message),
            # Plain text avoids Markdown formatting failures from dynamic values.
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                ok = response.status == 200 and isinstance(result, dict) and result.get("ok") is True
                if not ok:
                    print("[Telegram] API did not confirm message delivery.")
                return ok
        except Exception as exc:  # noqa: BLE001 - notifikasi tidak boleh pernah menjatuhkan robot
            # Do not print request URL or str(exc): they can contain the secret token.
            print(f"[Telegram] Send failed ({type(exc).__name__}).")
            return False

    def send_signal(self, side: str, price: float, reason: str = "",
                    dedupe_seconds: int = 900) -> bool:
        """Kirim sinyal dengan deduplikasi persisten selama 15 menit."""
        import fcntl
        import json
        import time
        from pathlib import Path

        side_key = str(side).upper().strip()
        reason_key = str(reason).strip()
        key = side_key + "|" + reason_key

        state_path = Path(__file__).resolve().parent.parent / "state" / "telegram_signal_dedupe.json"
        lock_path = state_path.with_suffix(".lock")
        state_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(lock_path, "a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

                if state_path.exists():
                    try:
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                        if not isinstance(state, dict):
                            raise ValueError("invalid state")
                    except Exception:
                        print("[Telegram] State dedupe rusak; pengiriman ditahan.")
                        return False
                else:
                    state = {}

                now = time.time()
                previous = float(state.get(key, 0) or 0)

                if previous and now - previous < max(1, int(dedupe_seconds)):
                    print("[Telegram] Sinyal duplikat ditahan.")
                    return False

                message = chr(10).join([
                    "OLYPHANTS — SIGNAL",
                    "Side: " + side_key,
                    "Price: " + format(float(price), ".2f") + " USDT",
                    "Reason: " + (reason_key or "Not provided"),
                ])

                result = self.send_message(message)
                if result is False:
                    return False

                state[key] = now
                state = {
                    k: v for k, v in state.items()
                    if isinstance(v, (int, float)) and now - v < 7 * 86400
                }

                tmp_path = state_path.with_suffix(".tmp")
                tmp_path.write_text(
                    json.dumps(state, sort_keys=True),
                    encoding="utf-8"
                )
                tmp_path.replace(state_path)
                return True

        except Exception as exc:
            print("[Telegram] Dedupe gagal: " + type(exc).__name__)
            return False


    def send_trade_open(
        self, side: str, price: float, qty: float, sl: float, tp: float | None
    ) -> bool:
        tp_text = f"{tp:.2f} USDT" if tp is not None else "Not set"
        return self.send_message(
            "OLYPAHANTSTRADE — POSITION OPENED\n"
            f"Side: {side.upper()}\nEntry: {price:.2f} USDT\n"
            f"Quantity: {qty}\nStop-loss: {sl:.2f} USDT\n"
            f"Take-profit: {tp_text}"
        )

    def send_trade_close(
        self, side: str, entry_price: float, exit_price: float,
        pnl: float, reason: str, fees: float | None = None
    ) -> bool:
        fee_line = f"\nFees: {fees:.4f} USDT" if fees is not None else ""
        return self.send_message(
            "OLYPAHANTSTRADE — POSITION CLOSED\n"
            f"Side: {side.upper()}\nEntry: {entry_price:.2f} USDT\n"
            f"Exit: {exit_price:.2f} USDT\n"
            f"Realized PnL: {pnl:+.4f} USDT{fee_line}\n"
            f"Exit reason: {reason}"
        )

    def send_halt(self, reason: str, drawdown_pct: float | None = None) -> bool:
        dd_line = (
            f"\nDrawdown: {drawdown_pct:.2f}%"
            if drawdown_pct is not None else ""
        )
        return self.send_message(
            "OLYPAHANTSTRADE — HALT / RISK ALERT\n"
            f"Reason: {reason}{dd_line}\n"
            "New entries must remain blocked until an authorized reset."
        )

    def send_status(self, status: dict[str, Any]) -> bool:
        position = status.get("position", "None")
        equity = status.get("equity", "unknown")
        halted = status.get("halted", False)
        reason = status.get("reason", "not provided")
        return self.send_message(
            "OLYPAHANTSTRADE — STATUS REPORT\n"
            f"Position: {position}\nEquity: {equity}\n"
            f"Halted: {halted}\nEvaluation: {reason}"
        )
