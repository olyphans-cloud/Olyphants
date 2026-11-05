#!/usr/bin/env python3
"""Bangun docs/GATE_EVIDENCE.md, docs/GATE_MATRIX.md, docs/TEST_INVENTORY.md, docs/gate_evidence.json.

- Pemetaan modul -> tes diturunkan dari impor NYATA di berkas tes (AST, termasuk berkas pembantu tests/_*.py), lalu
  tes dijalankan ulang. Jumlah tes lulus/gagal/dilewati diambil dari eksekusi nyata.
- Skrip ini TIDAK PERNAH meluluskan gerbang. Aturan proyek: LULUS hanya setelah audit independen DAN persetujuan
  pengguna. Karena itu kolom Status memakai BELUM DIUJI/GAGAL, bukan LULUS.

Jalankan dari folder olypahantstrade/:  python3 scripts/gate_evidence.py
"""
import ast
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PKG, TESTS = os.path.join(ROOT, "btc_robot"), os.path.join(ROOT, "tests")

KIND = {
    "test_core": "unit", "test_safeguard": "unit", "test_telegram_notifier": "unit", "test_mtf_strategy": "unit",
    "test_wallet_and_architecture": "arsitektur", "test_roadmap_modules": "unit", "test_v030_config_warnings": "unit",
    "test_v030_m1_audit": "audit implementasi", "test_paper": "integrasi", "test_fixes_v011": "integrasi",
    "test_v020_safeguard_wiring": "integrasi", "test_v020_modes": "integrasi", "test_v020_portfolio": "integrasi",
    "test_v030_pipeline": "integrasi", "test_v030_isolation": "integrasi/isolasi", "test_v030_dashboard": "integrasi/UI-API",
    "test_v030_data_secrets": "integrasi/kegagalan data+secret", "test_recovery": "kegagalan/recovery",
    "test_v020_failure_recovery": "kegagalan/recovery",
}

NOT_COVERED = {
    "data": "Koneksi nyata ke Binance (klines & bookTicker) belum pernah diuji; hanya server tiruan lokal (15 skenario kegagalan).",
    "engine": "Spread saat fill nyata tidak ada di backtest; trailing/max-hold bergranularitas candle, bukan tick.",
    "execution": "Hanya adapter SIMULASI. Tidak ada adapter order bursa; 'status order tidak diketahui' hanya disimulasikan.",
    "statemachine": "Terhubung ke engine dan state paper (tes pipeline/persistensi). Belum diuji terhadap bursa karena tidak ada adapter bursa.",
    "paper": "Runner paper = satu sleeve per proses (state terpisah per mode/koin lewat identitas). Belum ada runner paper portofolio.",
    "portfolio": "Baru simulasi backtest (kausal). Belum ada runner PAPER portofolio dengan state bersama.",
    "dashboard": "Tampilan visual di browser sungguhan BELUM diuji; yang teruji: API HTTP, keamanan, dan script halaman di Node dengan DOM tiruan.",
    "safeguard": "Pelepasan halt manual; belum ada pemantau eksternal (heartbeat) yang memicu HALT otomatis.",
    "recovery": "Pemulihan hanya PAPER; belum ada rekonsiliasi posisi terhadap bursa sungguhan.",
    "strategy": "Mode H1/M1 diuji pada data SINTETIS; kelayakan pada pasar nyata belum dibuktikan.",
    "mtf_strategy": "Idem: hanya data sintetis.",
    "backtest": "Hanya data sintetis; belum ada riwayat nyata multi-tahun di dalam bukti.",
    "validation": "Holdout belum dipakai pada data nyata.",
    "telegram_notifier": "Hanya server tiruan; Telegram sungguhan belum diuji. Hanya KIRIM notifikasi, tidak menerima perintah.",
}

# status: BELUM DIUJI | GAGAL | LULUS (LULUS tidak pernah ditetapkan oleh skrip ini)
GATES = [
    ("G0", "Specification", "Spesifikasi dan keputusan pemilik (angka risiko, spot/futures, prosedur darurat, kriteria G4/G5) tertulis dan disetujui.",
     "config.py (validasi batas), AUDIT_DOC01.md, ROADMAP_GAP_ANALYSIS.md",
     "test_core (validasi config), test_v030_config_warnings",
     "Batas risiko tervalidasi di kode. Keputusan pemilik tertulis belum ada (AUDIT_DOC01 bagian C).",
     "AUDIT_DOC01.md", "BELUM DIUJI", "Pemilik memutuskan butir AUDIT_DOC01 C; tanpa itu G0 tidak bisa lulus."),
    ("G1", "Data", "Data valid, lengkap, tanpa duplikat/celah/basi; kegagalan API ditangani aman.",
     "data.py, scripts/download_btc_history.py, scripts/verify_live_data.py",
     "test_v030_data_secrets (FetchFault, FetchToPaper, Csv, VerifyLiveData), test_core",
     "Lulus pada server tiruan (15 skenario kegagalan) dan CSV. Pengambilan data PASAR NYATA tidak dijalankan (sandbox tanpa internet).",
     "docs/GATE_EVIDENCE.md; tes", "BELUM DIUJI",
     "Jalankan `python3 scripts/verify_live_data.py` dan unduh riwayat nyata; kirim hasilnya."),
    ("G2", "Research (backtest anti-leakage + cost model + WFO/OOS)", "Backtest tanpa kebocoran, biaya realistis, walk-forward dan holdout di luar sampel menunjukkan dasar layak.",
     "backtest.py, validation.py, strategy.py (H1/M1), mtf_strategy.py, scripts/analyze_m1.py",
     "test_v030_m1_audit, test_v020_modes, test_mtf_strategy, test_core, test_fixes_v011",
     "Mekanisme anti-leakage dan model biaya lulus (termasuk mutation check). Hasil strategi hanya data SINTETIS: "
     "M1 negatif di 4/4 jendela WF dan holdout; H1 3/4 jendela positif, holdout kecil negatif (9 transaksi); MTF tidak dievaluasi di sini.",
     "docs/M1_H1_REPORT.md; docs/m1_audit/*", "GAGAL (M1, pada bukti yang ada) / BELUM DIUJI (H1, MTF)",
     "Evaluasi pada data nyata multi-tahun. M1: biaya ≈ jarak stop; perlu bukti edge nyata sebelum lanjut."),
    ("G3", "System (integration/E2E/failure/recovery)", "Integrasi, E2E, kegagalan, dan recovery teruji; jalur order melewati Risk/SafeGuard/HALT.",
     "engine.py (pipeline), statemachine.py, paper.py, recovery.py, identity.py, safeguard.py, dashboard.py",
     "test_v030_pipeline, test_v030_isolation, test_v020_failure_recovery, test_v020_safeguard_wiring, test_v030_dashboard, test_recovery, test_paper",
     "Semua lulus (lihat TEST_INVENTORY), dengan mutation check. Celah: tanpa adapter order bursa, tanpa runner paper portofolio, tanpa heartbeat HALT otomatis.",
     "docs/TEST_INVENTORY.md; docs/AUDIT_V5.md", "BELUM DIUJI",
     "Audit independen ChatGPT atas ZIP; tutup celah di REMAINING_ISSUES.md."),
    ("G4", "Shadow (pipeline live tanpa order)", "Pipeline berjalan pada data live tanpa mengirim order.",
     "(belum ada pipeline shadow khusus)", "-", "Belum diimplementasikan/dijalankan.", "-", "BELUM DIUJI", "Bangun dan jalankan pipeline shadow pada data live."),
    ("G5", "Paper (data nyata + order virtual)", "Siklus order virtual penuh pada data pasar nyata selama periode evaluasi.",
     "paper.py, dashboard.py, scripts/run_paper_24h.sh", "test_paper, test_v030_data_secrets (paritas backtest-PAPER H1/M1), test_v030_dashboard",
     "Order virtual lulus pada data sintetis/tiruan; paritas backtest-PAPER terbukti. Data NYATA belum dijalankan.", "tes", "BELUM DIUJI",
     "Jalankan paper berminggu-minggu pada data nyata; kirim log dan state."),
    ("G6", "Testnet (order/fill/recovery nyata di testnet)", "Order, fill, dan recovery nyata di testnet bursa.",
     "(TESTNET sengaja terkunci di config.py)", "-", "Tidak ada adapter order bursa.", "-", "BELUM DIUJI", "Perlu adapter bursa + persetujuan eksplisit pengguna."),
    ("G7", "Canary (live modal minimal)", "Live modal minimal.", "(LIVE terkunci: gates.py)", "-", "Bergantung pada G0–G6.", "-", "BELUM DIUJI", "Setelah G0–G6 lulus dan pengguna menyetujui."),
    ("G8", "Controlled Live (ekspansi)", "Ekspansi terkendali.", "(LIVE terkunci)", "-", "Bergantung pada G7.", "-", "BELUM DIUJI", "Setelah G7."),
]

NOT_RUN = [
    "Pengambilan data pasar NYATA (klines, bookTicker) — sandbox tanpa internet; skrip `scripts/verify_live_data.py` disediakan untuk perangkat Anda.",
    "Backtest, walk-forward, dan holdout pada data BTC nyata (hanya sintetis yang dijalankan).",
    "Paper trading berhari/berminggu pada data nyata.",
    "Telegram ke API sungguhan (hanya server tiruan).",
    "Tampilan dashboard di browser sungguhan (hanya HTTP API + script halaman di Node dengan DOM tiruan).",
    "Lintas platform: hanya Python 3.12 di Linux; belum Termux/Windows/macOS/Python 3.10–3.11.",
    "Dua proses OS terpisah menjalankan M1 dan H1 serentak (diuji: dua thread satu proses + lock berkas + state terpisah).",
    "Testnet/LIVE (terkunci; tidak ada adapter bursa).",
    "Kegagalan perangkat keras/jaringan sungguhan (disk penuh disimulasikan dengan injeksi os.replace).",
]


def imported_modules(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            parts = n.module.split(".")
            if parts[0] == "btc_robot":
                if len(parts) > 1:
                    mods.add(parts[1])
                else:
                    mods.update(a.name for a in n.names)
            elif parts[0] == "tests" and len(parts) > 1:
                helper = os.path.join(TESTS, parts[1] + ".py")
                if os.path.exists(helper):
                    mods |= imported_modules(helper)
        elif isinstance(n, ast.Import):
            for a in n.names:
                p = a.name.split(".")
                if p[0] == "btc_robot" and len(p) > 1:
                    mods.add(p[1])
    return mods


def run_module(name):
    suite = unittest.defaultTestLoader.loadTestsFromName(f"tests.{name}")
    res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    return {"tests": res.testsRun, "failures": len(res.failures), "errors": len(res.errors), "skipped": len(res.skipped),
            "passed": res.testsRun - len(res.failures) - len(res.errors) - len(res.skipped)}


def main():
    test_files = sorted(f[:-3] for f in os.listdir(TESTS) if f.startswith("test_") and f.endswith(".py"))
    results = {t: run_module(t) for t in test_files}
    imports = {t: imported_modules(os.path.join(TESTS, t + ".py")) for t in test_files}
    modules = sorted(f[:-3] for f in os.listdir(PKG) if f.endswith(".py") and f not in ("__init__.py", "__main__.py"))
    rows = []
    for m in modules:
        tfs = [t for t in test_files if m in imports[t]]
        rows.append({"module": m, "test_files": tfs, "kinds": sorted({KIND.get(t, "?") for t in tfs}),
                     "tests_in_files": sum(results[t]["tests"] for t in tfs),
                     "failing": sum(results[t]["failures"] + results[t]["errors"] for t in tfs),
                     "not_covered": NOT_COVERED.get(m, "")})
    tot = {k: sum(r[k] for r in results.values()) for k in ("tests", "passed", "failures", "errors", "skipped")}
    from btc_robot.config import CODE_VERSION
    D = os.path.join(ROOT, "docs")
    os.makedirs(D, exist_ok=True)

    # ---- GATE_EVIDENCE.md
    out = [f"# olypahantstrade — bukti per modul (kode v{CODE_VERSION})", "",
           "Dibuat otomatis oleh `scripts/gate_evidence.py`; jangan disunting tangan.", "",
           f"**Ringkasan tes:** {tot['tests']} tes, {tot['passed']} lulus, {tot['failures'] + tot['errors']} gagal/error, {tot['skipped']} dilewati.", "",
           "Kolom 'tes di berkas' = jumlah tes dalam berkas yang mengimpor modul (keterkaitan, bukan jaminan cakupan penuh).", "",
           "| Modul | Jenis bukti | Berkas tes | Tes di berkas | Gagal | Belum tercakup |", "|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| `{r['module']}` | {', '.join(r['kinds']) or '**tidak ada**'} | {', '.join(r['test_files']) or '-'} | "
                   f"{r['tests_in_files']} | {r['failing']} | {r['not_covered'] or '-'} |")
    with open(os.path.join(D, "GATE_EVIDENCE.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")

    # ---- GATE_MATRIX.md
    mx = [f"# Matriks gerbang G0–G8 (kode v{CODE_VERSION})", "",
          "Aturan status: **LULUS** hanya boleh ditetapkan setelah audit independen ChatGPT DAN persetujuan pengguna. "
          "Claude tidak menetapkan LULUS atas hasil sendiri, sehingga tidak ada gerbang berstatus LULUS di sini. "
          "**GAGAL** = kriteria diuji dan gagal pada bukti yang ada. **BELUM DIUJI** = belum diuji sah (termasuk 'lulus tes sendiri, menunggu audit independen').", "",
          f"Hasil tes mandiri saat laporan dibuat: {tot['passed']}/{tot['tests']} lulus. Berkas gerbang (`state/release_gates.json`) tidak diubah.", "",
          "| Gerbang | Persyaratan | Implementasi terkait | Tes yang dijalankan | Hasil aktual | Bukti | Status | Perbaikan lanjutan |",
          "|---|---|---|---|---|---|---|---|"]
    for g, name, req, impl, tests, actual, ev, status, nxt in GATES:
        mx.append(f"| **{g}** {name} | {req} | {impl} | {tests} | {actual} | {ev} | **{status}** | {nxt} |")
    with open(os.path.join(D, "GATE_MATRIX.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(mx) + "\n")

    # ---- TEST_INVENTORY.md
    inv = [f"# Inventaris tes (kode v{CODE_VERSION})", "",
           f"**Total: {tot['tests']} tes — {tot['passed']} lulus, {tot['failures'] + tot['errors']} gagal, {tot['skipped']} dilewati.** "
           "Lingkungan: Python 3.12, Linux, hanya pustaka standar (tanpa instalasi paket).", "",
           "## Dijalankan", "", "| Berkas tes | Jenis | Tes | Lulus | Gagal | Dilewati |", "|---|---|---|---|---|---|"]
    for t in test_files:
        r = results[t]
        inv.append(f"| `{t}` | {KIND.get(t, '?')} | {r['tests']} | {r['passed']} | {r['failures'] + r['errors']} | {r['skipped']} |")
    inv += ["", "## Belum dijalankan (tidak ada klaim hasil)", ""] + [f"- {x}" for x in NOT_RUN]
    with open(os.path.join(D, "TEST_INVENTORY.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(inv) + "\n")
    with open(os.path.join(D, "gate_evidence.json"), "w", encoding="utf-8") as fh:
        json.dump({"code_version": CODE_VERSION, "totals": tot, "test_files": results, "modules": rows,
                   "gates": {g[0]: g[7] for g in GATES}, "gates_passed": []}, fh, indent=2, sort_keys=True)
    print(f"{tot['tests']} tes, {tot['passed']} lulus, {tot['failures'] + tot['errors']} gagal, {tot['skipped']} dilewati")
    return 0 if tot["failures"] + tot["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
