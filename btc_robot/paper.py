"""Paper trading: satu langkah per pemanggilan (cocok untuk cron/Termux). Order hanya disimulasikan.

Alur: ambil klines -> buang candle belum tutup -> validasi (umur & kelengkapan) -> proses candle baru
-> simpan state. Data gagal/basi = tidak ada entri baru (fail-safe), dicatat di audit.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditLog
from .config import Config, Mode, config_hash, validate
from .identity import check_identity, write_identity
from .data import DataError, closed_only, fetch_tokocrypto_klines, validate_candles, fetch_tokocrypto_book_ticker
from .statemachine import State
from .engine import Engine, signal_from_dict, signal_to_dict
from .execution import SimulatedExecutionAdapter
from .ledger import Ledger
from .models import BookQuote, Candle
from .risk import RiskEngine
from .strategies import make_strategy
from .safeguard import SafeguardExecutionEngine
from .telegram_notifier import TelegramNotifier

try:  # POSIX (Linux/Termux/macOS); di Windows lock dilewati
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


@contextlib.contextmanager
def _file_lock(path: str):
    """Lock non-blocking agar dua paper-step tidak berjalan bersamaan (state rusak/ganda)."""
    if fcntl is None:
        yield True
        return
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def _state_path(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, "paper_state.json")


def paper_step(cfg: Config, now_ms: Optional[int] = None,
               fetcher: Optional[Callable[[], List[Candle]]] = None,
               spread_provider: Optional[Callable[[Candle], Optional[BookQuote]]] = None) -> Dict[str, Any]:
    cfg = validate(cfg)
    if cfg.mode != Mode.PAPER:
        raise ValueError("paper_step hanya untuk mode PAPER")
    id_ok, id_status, id_msg = check_identity(cfg)  # baca-saja; penolakan tidak menyentuh direktori apa pun
    if not id_ok:
        return {"status": id_status, "error": id_msg}
    os.makedirs(cfg.state_dir, exist_ok=True)
    with _file_lock(os.path.join(cfg.state_dir, ".paper.lock")) as got:
        if not got:
            return {"status": "locked", "error": "paper-step lain sedang berjalan; langkah ini dilewati"}
        return _paper_step_impl(cfg, now_ms, fetcher, spread_provider)


def _paper_step_impl(cfg: Config, now_ms: Optional[int] = None,
                     fetcher: Optional[Callable[[], List[Candle]]] = None,
                     spread_provider: Optional[Callable[[Candle], Optional[BookQuote]]] = None) -> Dict[str, Any]:
    cfg = validate(cfg)
    if cfg.mode != Mode.PAPER:
        raise ValueError("paper_step hanya untuk mode PAPER")
    id_ok, id_status, id_msg = check_identity(cfg)
    if not id_ok:  # tolak aman: TIDAK menulis apa pun (audit/state mode lain tidak boleh tercemar)
        return {"status": id_status, "error": id_msg}
    os.makedirs(cfg.state_dir, exist_ok=True)
    if id_status == "fresh":
        write_identity(cfg)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    audit = AuditLog(os.path.join(cfg.state_dir, "paper_audit.jsonl"), config_hash(cfg))

    try:
        raw = fetcher() if fetcher else fetch_tokocrypto_klines(cfg.symbol, cfg.interval, 500)
    except Exception as exc:  # noqa: BLE001 - sumber data apa pun yang gagal: state TIDAK disentuh
        audit.log("data_fetch_failed", now_ms, error=f"{type(exc).__name__}: {exc}")
        return {"status": "no_data", "error": f"{type(exc).__name__}: {exc}"}

    candles = closed_only(raw, cfg.interval_ms, now_ms)
    report = validate_candles(candles, cfg.interval_ms, now_ms=now_ms,
                              max_age_seconds=cfg.risk.max_data_age_seconds)
    if not report.ok:
        audit.log("data_invalid", now_ms, issues=report.issues[:10])
        return {"status": "data_invalid", "issues": report.issues[:10]}

    strategy = make_strategy(cfg)
    strategy.prepare(candles)
    risk = RiskEngine(cfg, Mode.PAPER)
    adapter = SimulatedExecutionAdapter(cfg.costs, Mode.PAPER, risk.verify)

    path = _state_path(cfg)
    state: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                raise ValueError("state root bukan object")
            ledger = Ledger.from_dict(state["ledger"]) if "ledger" in state else Ledger(cfg.initial_equity)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # State rusak TIDAK boleh diam-diam di-reset (itu menghapus posisi/risiko). Tolak & biarkan file apa adanya.
            audit.log("state_corrupt", now_ms, error=type(exc).__name__)
            return {"status": "state_corrupt", "error": f"state paper tidak terbaca ({type(exc).__name__}); "
                    "file tidak diubah, periksa/pulihkan manual"}
    else:
        ledger = Ledger(cfg.initial_equity)
    if state.get("halted"):
        risk.halt(state.get("halt_reason") or "halted_from_state")
    adapter.seen = {}  # idempotensi lintas-proses dijaga oleh last_ts + sinyal tunggal tertunda
    candidate_notifier = TelegramNotifier()
    notifier = candidate_notifier if candidate_notifier.enabled and candidate_notifier.token and candidate_notifier.chat_id else None
    if spread_provider is None and cfg.risk.require_spread_check:
        latest_ts, symbol = candles[-1].ts, cfg.symbol

        def spread_provider(bar: Candle) -> Optional[BookQuote]:  # noqa: F811
            # Hanya candle terbaru: order book historis tidak tersedia -> candle lama = 'tidak diketahui' (ditolak).
            # CATATAN: ini spread saat pemrosesan (~close candle), proksi untuk spread saat fill di open.
            return fetch_tokocrypto_book_ticker(symbol) if bar.ts == latest_ts else None

    safeguard = SafeguardExecutionEngine(max_spread_pct=cfg.risk.max_spread_pct,
                                         max_loss_streak=cfg.risk.max_loss_streak,
                                         cooldown_sec=cfg.risk.loss_streak_cooldown_sec,
                                         max_orderbook_age_sec=cfg.risk.max_orderbook_age_sec,
                                         state_path=os.path.join(cfg.state_dir, "safeguard_state.json"))
    # state machine: pulihkan dari penyimpanan, dan WAJIB konsisten dengan ledger/pending (jika tidak: tolak)
    has_pos, has_pending = ledger.position is not None, bool(state.get("pending"))
    saved_ms = state.get("machine_state")
    if saved_ms is None and (has_pos or has_pending) :   # state lama (v4): turunkan dari ledger
        saved_ms = State.POSITION.value if has_pos else State.SIGNAL.value
    if saved_ms is not None:
        try:
            ok_ms = State(saved_ms) in Engine.expected_machine_states(has_pos, has_pending)
        except ValueError:
            ok_ms = False
        if not ok_ms:
            audit.log("state_inconsistent", now_ms, machine_state=saved_ms, has_position=has_pos, has_pending=has_pending)
            return {"status": "state_inconsistent",
                    "error": f"machine_state={saved_ms} tidak cocok dengan ledger (posisi={has_pos}, pending={has_pending}); "
                             "state tidak diubah, periksa manual"}
    engine = Engine(cfg, Mode.PAPER, strategy, risk, adapter, ledger, audit, notifier=notifier, safeguard=safeguard,
                    spread_provider=spread_provider, machine_state=saved_ms)
    if state.get("pending"):
        engine.pending = signal_from_dict(state["pending"])
    engine.last_entry_hour = state.get("last_entry_hour")
    engine.entries_in_hour = int(state.get("entries_in_hour", 1 if engine.last_entry_hour is not None else 0))

    gap_missed = 0
    last_ts = state.get("last_ts")
    if last_ts is None:  # inisialisasi: mulai dari candle terakhir, jangan backfill trade historis
        last_ts = candles[-1].ts
        processed = 0
        audit.log("paper_init", now_ms, start_after_ts=last_ts)
    else:
        processed = 0
        # Deteksi celah: candle antara last_ts dan awal jendela tidak pernah diproses (cron mati lama).
        first_new = next((c.ts for c in candles if c.ts > last_ts), None)
        expected = last_ts + cfg.interval_ms
        if first_new is not None and first_new > expected:
            gap_missed = (first_new - expected) // cfg.interval_ms
            if ledger.position is not None:
                # Stop/TP pada candle yang terlewat tidak bisa dipastikan: latch halt untuk tinjauan manusia.
                risk.halt(f"data_gap:{gap_missed}_candle_terlewat_saat_posisi_terbuka")
                audit.log("data_gap_halt", now_ms, missed_bars=gap_missed, last_ts=last_ts, first_new_ts=first_new)
            else:
                engine.pending = None  # sinyal lama sudah basi
                audit.log("data_gap_skipped", now_ms, missed_bars=gap_missed, last_ts=last_ts, first_new_ts=first_new)
        for i, c in enumerate(candles):
            if c.ts > last_ts:
                engine.process_bar(candles, i)
                last_ts = c.ts
                processed += 1

    mark = candles[-1].close
    new_state = {"ledger": ledger.to_dict(), "last_ts": last_ts,
                 "last_equity": ledger.equity(mark), "last_mark": mark,
                 "pending": signal_to_dict(engine.pending) if engine.pending else None,
                 "halted": risk.halted, "halt_reason": risk.halt_reason,
                 "machine_state": engine.machine.state.value,
                 "last_entry_hour": engine.last_entry_hour, "entries_in_hour": engine.entries_in_hour,
                 "safeguard": safeguard.status(now=now_ms / 1000.0)}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(new_state, fh, sort_keys=True)
    os.replace(tmp, path)  # tulis atomik
    return {"status": "ok", "processed_bars": processed, "last_ts": last_ts, "gap_missed_bars": gap_missed,
            "equity": ledger.equity(mark), "position": ledger.position.side.value if ledger.position else None,
            "halted": risk.halted or safeguard.halted, "safeguard": safeguard.status(now=now_ms / 1000.0),
            "counters": dict(engine.counters)}
