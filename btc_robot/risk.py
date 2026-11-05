"""Risk engine: SATU-SATUNYA yang menyetujui ukuran/kelayakan transaksi (Dok 01 bagian 6).

Order yang disetujui ditandatangani HMAC dengan kunci acak per-proses milik engine ini.
Adapter eksekusi memverifikasi tanda tangan, sehingga strategi/AI tidak bisa membuat order sendiri.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
from typing import Dict, Optional, Tuple

from .config import Config, Mode
from .models import ApprovedOrder, PortfolioSnapshot, RiskDecision, Side, Signal


def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        raise ValueError("step harus > 0")
    return math.floor(x / step + 1e-9) * step


class RiskEngine:
    def __init__(self, cfg: Config, mode: Mode):
        self.cfg = cfg
        self.mode = mode
        self._key = secrets.token_bytes(32)
        self.halted: bool = False
        self.halt_reason: Optional[str] = None

    # ---- kontrol darurat -------------------------------------------------
    def halt(self, reason: str) -> None:
        """Latch: tetap berhenti sampai manusia membuat engine baru (restart terkontrol)."""
        self.halted = True
        self.halt_reason = reason

    def _kill_file_present(self) -> bool:
        shared = self.cfg.shared_kill_switch_file
        return os.path.exists(self.cfg.kill_switch_file) or bool(shared and os.path.exists(shared))

    # ---- tanda tangan ----------------------------------------------------
    def _sign(self, o_id: str, side: Side, qty: float, ref: float, stop: float,
              tp: Optional[float], ts: int, mode: str) -> str:
        msg = f"{o_id}|{side.value}|{qty:.10f}|{ref:.10f}|{stop:.10f}|{tp if tp is None else format(tp, '.10f')}|{ts}|{mode}"
        return hmac.new(self._key, msg.encode(), hashlib.sha256).hexdigest()

    def verify(self, order: ApprovedOrder) -> bool:
        try:
            expected = self._sign(order.client_order_id, order.side, order.qty, order.ref_price,
                                  order.stop_price, order.take_profit, order.signal_bar_ts, order.mode)
            return hmac.compare_digest(expected, order.signature)
        except Exception:
            return False

    def _client_order_id(self, sig: Signal) -> str:
        raw = f"{sig.strategy_version}|{sig.bar_ts}|{sig.side.value}|{self.mode.value}|{sig.data_fingerprint}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    # ---- evaluasi --------------------------------------------------------
    def evaluate(self, sig: Signal, ref_price: float, snap: PortfolioSnapshot,
                 data_ok: bool, bars_since_signal: int = 1) -> Tuple[RiskDecision, Optional[ApprovedOrder]]:
        """Fail-safe: error apa pun => ditolak."""
        try:
            return self._evaluate(sig, ref_price, snap, data_ok, bars_since_signal)
        except Exception as exc:  # noqa: BLE001 - sengaja menangkap semua
            return RiskDecision(False, f"risk_engine_error:{type(exc).__name__}", 0.0, {}), None

    def _reject(self, reason: str, checks: Dict[str, object]) -> Tuple[RiskDecision, None]:
        checks[reason] = False
        return RiskDecision(False, reason, 0.0, checks), None

    def _evaluate(self, sig, ref_price, snap, data_ok, bars_since_signal):
        r, c = self.cfg.risk, self.cfg.costs
        checks: Dict[str, object] = {}

        if self.mode not in (Mode.BACKTEST, Mode.PAPER):
            return self._reject("mode_tidak_diizinkan", checks)
        if self.halted:
            return self._reject(f"halted:{self.halt_reason}", checks)
        if self._kill_file_present():
            return self._reject("kill_switch_file", checks)
        if not data_ok:
            return self._reject("data_tidak_valid", checks)
        if sig.side == Side.NO_TRADE:
            return self._reject("tidak_ada_sinyal", checks)
        if sig.side == Side.SHORT and not r.allow_short:
            return self._reject("short_tidak_diizinkan", checks)
        if bars_since_signal > r.max_signal_age_bars:
            return self._reject("sinyal_basi", checks)
        if snap.open_positions >= r.max_open_positions:
            return self._reject("posisi_sudah_terbuka", checks)
        for name, v in (("ref_price", ref_price), ("equity", snap.equity), ("cash", snap.cash)):
            if not (isinstance(v, (int, float)) and math.isfinite(v)):
                return self._reject(f"{name}_non_finite", checks)
        if ref_price <= 0 or snap.equity <= 0:
            return self._reject("harga_atau_ekuitas_tidak_valid", checks)
        if sig.stop_price is None or not math.isfinite(sig.stop_price):
            return self._reject("stop_tidak_ada", checks)

        # stop harus di sisi yang benar relatif harga referensi saat ini (menangkap gap)
        long = sig.side == Side.LONG
        if (long and sig.stop_price >= ref_price) or (not long and sig.stop_price <= ref_price):
            return self._reject("stop_di_sisi_salah", checks)
        if sig.take_profit is not None:
            if (long and sig.take_profit <= ref_price) or (not long and sig.take_profit >= ref_price):
                return self._reject("tp_di_sisi_salah", checks)
        stop_dist = abs(ref_price - sig.stop_price)
        stop_pct = stop_dist / ref_price * 100.0
        checks["stop_pct"] = round(stop_pct, 4)
        if not (r.min_stop_distance_pct <= stop_pct <= r.max_stop_distance_pct):
            return self._reject("jarak_stop_di_luar_batas", checks)

        # edge harus menutup biaya pulang-pergi (fee + spread + slippage) dengan margin
        if r.min_edge_cost_mult > 0 and sig.take_profit is not None:
            rt_cost_pct = 2.0 * (c.fee_rate * 100.0 + (c.spread_bps / 2.0 + c.slippage_bps) / 100.0)
            tp_pct = abs(sig.take_profit - ref_price) / ref_price * 100.0
            checks["tp_pct"], checks["rt_cost_pct"] = round(tp_pct, 4), round(rt_cost_pct, 4)
            if tp_pct < r.min_edge_cost_mult * rt_cost_pct:
                return self._reject("edge_tidak_cukup_untuk_biaya", checks)

        # batas kerugian harian & drawdown
        if snap.day_realized_pnl <= -snap.day_start_equity * r.max_daily_loss_pct / 100.0:
            return self._reject("batas_rugi_harian", checks)
        dd = (snap.peak_equity - snap.equity) / snap.peak_equity * 100.0 if snap.peak_equity > 0 else 0.0
        checks["drawdown_pct"] = round(dd, 4)
        if dd >= r.max_drawdown_pct:
            self.halt(f"max_drawdown {dd:.2f}%")
            return self._reject("max_drawdown_halt", checks)

        # ukuran posisi: risiko termasuk biaya pulang-pergi (pendekatan)
        side_cost = ref_price * (c.fee_rate + (c.spread_bps / 2.0 + c.slippage_bps) / 10_000.0)
        loss_per_unit = stop_dist + 2.0 * side_cost
        qty_risk = snap.equity * r.risk_per_trade_pct / 100.0 / loss_per_unit
        qty_notional = snap.equity * r.max_notional_pct_of_equity / 100.0 / ref_price
        qty = min(qty_risk, qty_notional)
        if long:
            qty_cash = snap.cash / (ref_price * (1.0 + c.fee_rate + (c.spread_bps / 2.0 + c.slippage_bps) / 10_000.0))
            qty = min(qty, qty_cash)
        qty = floor_to_step(qty, c.lot_step)
        checks["qty"] = qty
        if qty < c.min_qty:
            return self._reject("qty_di_bawah_minimum", checks)
        if qty * ref_price < c.min_notional:
            return self._reject("notional_di_bawah_minimum", checks)

        o_id = self._client_order_id(sig)
        sig_hex = self._sign(o_id, sig.side, qty, ref_price, sig.stop_price, sig.take_profit,
                             sig.bar_ts, self.mode.value)
        order = ApprovedOrder(o_id, sig.side, qty, ref_price, sig.stop_price, sig.take_profit,
                              sig.bar_ts, self.mode.value, sig_hex)
        return RiskDecision(True, "disetujui", qty, checks), order
