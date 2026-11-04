#!/usr/bin/env python3
"""Reproduksi temuan audit terhadap pohon kode MANA PUN (v4 atau baru). Setiap pemeriksaan melaporkan
ADA (bug terjadi) atau TIDAK ADA.

  python3 scripts/repro_v4_bugs.py /path/ke/pohon_kode
"""
import json
import os
import subprocess
import sys

CHECKS = r'''
import sys, os, json, tempfile, io, contextlib
from dataclasses import replace
ROOT = sys.argv[1]
sys.path.insert(0, ROOT); os.chdir(ROOT)
res = {}

def check(name):
    def deco(fn):
        try: res[name] = bool(fn())
        except Exception as e: res[name] = "ERROR: %s: %s" % (type(e).__name__, str(e)[:80])
        return fn
    return deco

@check("1_state_dicampur_antar_mode (M1 memakai state H1 dan melapor ok)")
def _():
    from btc_robot.config import load_config, Mode
    from btc_robot.data import generate_synthetic
    from btc_robot.paper import paper_step
    d = tempfile.mkdtemp(); H, M = 3_600_000, 60_000
    h1 = replace(load_config("config.h1.example.json"), mode=Mode.PAPER, state_dir=d)
    m1 = replace(load_config("config.m1.example.json"), mode=Mode.PAPER, state_dir=d)
    ch = generate_synthetic(500, 3, H); cm = generate_synthetic(3000, 3, M, vol=0.0007)
    for k in range(300, 310): paper_step(h1, now_ms=ch[k].ts + H + 15000, fetcher=lambda k=k: ch[:k + 1])
    r = paper_step(m1, now_ms=cm[1500].ts + M + 15000, fetcher=lambda: cm[1000:1501])
    return r["status"] == "ok"

@check("2_urutan_pipeline: SafeGuard dipanggil SEBELUM Risk")
def _():
    from tests._v020_helpers import HOUR, base_cfg, flat_bars, make_engine, run_all
    bars = flat_bars(10, HOUR); eng = make_engine(base_cfg("1h"), {2: (1.0, 50.0)}, bars); log = []
    for obj, n, lab in ((eng.risk, "evaluate", "risk"), (eng.safeguard, "check_circuit_breaker", "safeguard")):
        o = getattr(obj, n); setattr(obj, n, (lambda o, lab: lambda *a, **k: (log.append(lab), o(*a, **k))[1])(o, lab))
    run_all(eng, bars)
    return log.index("safeguard") < log.index("risk")

@check("3_cooldown_bergantung_sinyal_yang_dievaluasi (hasil beda dari skenario berjangkar)")
def _():
    from tests._v020_helpers import HOUR, base_cfg, flat_bars, make_engine, run_all, with_bar
    bars = flat_bars(30, HOUR)
    for i in (3, 7, 11): with_bar(bars, i, 100, 100.05, 98.5, 98.8)
    plan = {2: (1.0, 50.0), 6: (1.0, 50.0), 10: (1.0, 50.0), 12: (1.0, -1.0), 14: (1.0, 50.0), 15: (1.0, 50.0)}
    eng = make_engine(base_cfg("1h"), plan, bars); run_all(eng, bars)
    return eng.counters["entries"] != 4

@check("4_statemachine_tidak_terhubung_ke_engine")
def _():
    from tests._v020_helpers import HOUR, base_cfg, flat_bars, make_engine
    return not hasattr(make_engine(base_cfg("1h"), {}, flat_bars(5, HOUR)), "machine")

@check("5_tidak_ada_pemeriksaan_HALT_final_sebelum_submit")
def _():
    from tests._v020_helpers import HOUR, base_cfg, flat_bars, make_engine
    return not hasattr(make_engine(base_cfg("1h"), {}, flat_bars(5, HOUR)), "_final_halt_reason")

@check("6_token_di_dalam_teks_error_lolos_redaksi_audit")
def _():
    from btc_robot.audit import AuditLog
    tok = "123456789" + ":" + "A" * 35
    d = tempfile.mkdtemp(); p = os.path.join(d, "a.jsonl")
    AuditLog(p, "h").log("x", 1, error="gagal https://api.telegram.org/bot" + tok + "/sendMessage")
    return tok in open(p).read()

@check("7_placeholder_PUT_NEW_TOKEN_HERE_dipakai_sebagai_token (percobaan jaringan)")
def _():
    import urllib.request
    from btc_robot.telegram_notifier import TelegramNotifier
    calls = []
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: (calls.append(1), (_ for _ in ()).throw(OSError("blok")))[1]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            TelegramNotifier(token="PUT_NEW_TOKEN_HERE", chat_id="42", enabled=True).send_message("x")
    finally: urllib.request.urlopen = orig
    return len(calls) > 0

@check("8_fetch_klines_melempar_TypeError_mentah_pada_baris_null")
def _():
    import urllib.request
    from btc_robot import data as D
    class R:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def read(s): return b"[null]"
    orig = urllib.request.urlopen; slp = D.time.sleep
    urllib.request.urlopen = lambda *a, **k: R(); D.time.sleep = lambda *_: None
    try:
        D.fetch_binance_klines("X", "1h", 5, retries=1); return False
    except D.DataError: return False
    except TypeError: return True
    finally: urllib.request.urlopen = orig; D.time.sleep = slp

@check("9_walkforward_tanpa_total_fees_per_jendela")
def _():
    from btc_robot.config import load_config, validate
    from btc_robot.data import generate_synthetic
    from btc_robot.validation import walk_forward
    w = walk_forward(generate_synthetic(1500, 1), validate(load_config("config.h1.example.json")), 2)
    return "total_fees" not in w[0]

print(json.dumps(res))
'''


def main():
    root = os.path.abspath(sys.argv[1])
    p = subprocess.run([sys.executable, "-c", CHECKS, root], capture_output=True, text=True, timeout=240)
    try:
        res = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print("gagal menjalankan:", p.stderr[-400:])
        return 2
    print(f"Pohon kode: {root}")
    for k, v in res.items():
        print(f"  [{'ADA  ' if v is True else 'TIDAK' if v is False else v}] {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
