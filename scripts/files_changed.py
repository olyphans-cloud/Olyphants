#!/usr/bin/env python3
"""Bandingkan dua pohon (v4 vs baru) dan tulis docs/FILES_CHANGED.md dengan alasan per berkas.

  python3 scripts/files_changed.py /path/v4 /path/baru
"""
import os
import sys

REASON = {
    "btc_robot/engine.py": "Pipeline order eksplisit HALT→Risk→SafeGuard→Portofolio→HALT final→eksekusi; integrasi StateMachine; begin_bar/end_bar; max-hold, trailing, spread, batas entry/jam; exec_unknown→RECONCILING.",
    "btc_robot/paper.py": "Cek identitas state sebelum menulis; persist/restore machine_state + validasi konsistensi; lock berkas; deteksi celah data; state rusak ditolak; last_equity untuk dashboard; fetch gagal apa pun→no_data.",
    "btc_robot/safeguard.py": "Cooldown berjangkar pada waktu rugi ke-3; pencatatan trade idempoten (replay setelah crash); release_halt tanpa reset loss streak.",
    "btc_robot/recovery.py": "Melihat latch HALT safeguard (bug buntu); cek identitas; RECONCILING→READY setelah persetujuan operator.",
    "btc_robot/statemachine.py": "restore(), log transisi rutin senyap, riwayat terbatas (agar bisa dipakai engine).",
    "btc_robot/risk.py": "Filter edge-vs-biaya; kill switch bersama.",
    "btc_robot/config.py": "Field risiko baru (cooldown, max-hold, trailing, spread, edge, entri/jam), mode h1_trend/m1_scalp, kill switch bersama, sleeve_id, config_warnings, versi 0.3.0.",
    "btc_robot/data.py": "fetch_book_ticker; fetch_klines menolak respons non-list & TypeError; vol di generator sintetis.",
    "btc_robot/audit.py": "Redaksi token Telegram yang muncul di dalam TEKS pesan error.",
    "btc_robot/telegram_notifier.py": "api_base (uji), placeholder PUT_* dianggap belum dikonfigurasi, semua error ditangani tanpa membocorkan token, nama robot.",
    "btc_robot/validation.py": "Warm-up walk-forward otomatis; jendela memuat total_fees/net_pnl/win_rate.",
    "btc_robot/backtest.py": "Memakai pabrik strategi (make_strategy).",
    "btc_robot/__main__.py": "Perintah portfolio-backtest, scan, adopt-state, dashboard; peringatan konfigurasi ke stderr.",
    "btc_robot/__init__.py": "Nama proyek.",
    "btc_robot/identity.py": "BARU: identitas state per (mode, simbol, interval); penolakan aman bila campur/rusak/lama.",
    "btc_robot/dashboard.py": "BARU: dashboard lokal PAPER (loopback, token, ACC tanpa order, statistik M1/H1 terpisah, AI Scan/Manual).",
    "btc_robot/portfolio.py": "BARU: scanner, gerbang risiko bersama, simulasi multi-sleeve kausal.",
    "btc_robot/strategies.py": "BARU: pabrik strategi per mode.",
    "btc_robot/strategy.py": "Strategi H1 dan M1 mandiri.",
    "btc_robot/mtf_strategy.py": "Bisect untuk _latest_closed (performa kuadratik→log).",
    "config.h1.example.json": "BARU/diubah: state_dir terpisah state/h1, kill switch bersama.",
    "config.m1.example.json": "BARU/diubah: state_dir terpisah state/m1, kill switch bersama.",
    "config.mtf.example.json": "state_dir terpisah state/mtf, kill switch bersama.",
    "portfolio.example.json": "BARU: contoh spesifikasi portofolio.",
    "scripts/analyze_m1.py": "BARU: rincian transaksi, atribusi, pembanding acak, regime, ablasi, sensitivitas, walk-forward.",
    "scripts/build_m1_report.py": "BARU: laporan M1/H1 dari hasil (tanpa angka manual).",
    "scripts/compare_versions.py": "BARU: perbandingan sebelum/sesudah pada data/biaya yang sama.",
    "scripts/gate_evidence.py": "BARU: bukti per modul, matriks gerbang, inventaris tes.",
    "scripts/package_check.py": "BARU: pemeriksa isi ZIP (secret/state/cache).",
    "scripts/verify_live_data.py": "BARU: uji data pasar publik di perangkat Anda (tanpa order).",
    "scripts/download_btc_history.py": "BARU: unduh riwayat klines.",
    "scripts/run_paper_24h.sh": "BARU: paper trading 24 jam.",
    "scripts/files_changed.py": "BARU: pembuat berkas ini.",
    "CHANGELOG.md": "Entri v0.3.0 (temuan, perbaikan, status).",
    "README.md": "Isolasi state, jalur order, dashboard, laporan; judul/nama proyek.",
    "SAFEGUARD_AUDIT.md": "Ditulis ulang: pipeline order, integrasi StateMachine, batas yang jujur.",
    "scripts/repro_v4_bugs.py": "BARU: reproduksi 9 temuan audit terhadap pohon kode mana pun (v4 vs baru).",
    "tests/test_recovery.py": "Fixture memakai identitas state (perilaku baru: state lama tanpa identitas ditolak).",
    "tests/test_v020_modes.py": "Golden hash MTF diperbarui dengan penjelasan (cooldown berjangkar).",
    "tests/test_v020_safeguard_wiring.py": "Menutup soket server tiruan.",
}


def tree(root):
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git")]
        for f in files:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = p
    return out


def main():
    old, new = tree(sys.argv[1]), tree(sys.argv[2])
    added = sorted(k for k in new if k not in old and not k.startswith(("state/", "docs/m1_audit/")))
    removed = sorted(k for k in old if k not in new)
    changed = sorted(k for k in new if k in old and open(new[k], "rb").read() != open(old[k], "rb").read())
    L = ["# Berkas yang berubah (v4 → v0.3.0)", "", "Dibuat otomatis oleh `scripts/files_changed.py` dari perbandingan isi berkas.", ""]
    for title, items in (("Diubah", changed), ("Baru", added), ("Dihapus", removed)):
        L += [f"## {title} ({len(items)})", "", "| Berkas | Alasan |", "|---|---|"]
        for k in items:
            if title == "Baru" and k.startswith("tests/test_v030") or k.startswith("docs/"):
                reason = REASON.get(k, "Tes baru untuk perbaikan v0.3.0" if k.startswith("tests/") else "Dokumen/laporan hasil (dibuat otomatis atau ditulis)")
            else:
                reason = REASON.get(k, "-")
            L.append(f"| `{k}` | {reason} |")
        L.append("")
    out = os.path.join(sys.argv[2], "docs", "FILES_CHANGED.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print(f"diubah={len(changed)} baru={len(added)} dihapus={len(removed)}")
    unexplained = [k for k in changed + added if k not in REASON and not k.startswith(("tests/test_v0", "docs/", "tests/js", "tests/_v0", "tests/__"))]
    if unexplained:
        print("TANPA ALASAN:", unexplained)


if __name__ == "__main__":
    main()
