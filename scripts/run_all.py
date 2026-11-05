#!/usr/bin/env python3
"""SATU PERINTAH: unduh data BTC/ETH/BNB/SOL -> uji strategi (long-only dan long+short) -> satu file HASIL_LONG_SHORT.txt.

  python3 scripts/run_all.py
Hasil: HASIL_LONG_SHORT.txt (juga disalin ke folder Download HP). Kirim file itu ke Claude.
"""
import os, shutil, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "scripts")
synthetic = "--synthetic" in sys.argv   # hanya untuk uji skrip
D = ["--synthetic"] if synthetic else ["--data", os.path.join(ROOT, "data")]


def run(title, args, out):
    out.write(f"\n===== {title} =====\n"); out.flush()
    print(f">> {title}", flush=True)
    p = subprocess.Popen([sys.executable] + args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        out.write(line); out.flush()
        print(line, end="", flush=True)
    return p.wait()


with open(os.path.join(ROOT, "HASIL_LONG_SHORT.txt"), "w", encoding="utf-8") as out:
    out.write(f"Olyphant's lab - {time.strftime('%Y-%m-%d %H:%M')} - {'SINTETIS' if synthetic else 'DATA ASLI'}\n")
    if not synthetic:
        if run("1. Unduh data", [os.path.join(S, "fetch_all.py"), "--years", "3", "--years-long", "5"], out) != 0:
            out.write("\nGAGAL unduh data (kemungkinan Binance diblokir). Coba jaringan/VPN lain.\n")
            sys.exit("Gagal unduh data. Lihat HASIL_LONG_SHORT.txt")
    lab, srch = os.path.join(S, "strategy_lab.py"), os.path.join(S, "strategy_search.py")
    ALL = "15m,1h,4h,1d"
    run("2. LONG+SHORT, 15m 1h 4h 1d: semua strategi + walk-forward (BTC ETH BNB SOL)", [lab] + D + ["--allow-short", "--tfs", ALL], out)
    run("3. LONG-ONLY, 15m 1h 4h 1d: semua strategi + walk-forward", [lab] + D + ["--tfs", ALL], out)
    run("4. LONG+SHORT, 15m 1h 4h 1d: pencarian parameter (latih/validasi/holdout)", [srch] + D + ["--allow-short", "--tfs", ALL], out)
    run("5. LONG-ONLY, 15m 1h 4h 1d: pencarian parameter (latih/validasi/holdout)", [srch] + D + ["--tfs", ALL], out)
    out.write("\nSELESAI. Kirim file HASIL_LONG_SHORT.txt ke Claude.\n")

dl = os.path.expanduser("~/storage/downloads")
if os.path.isdir(dl):
    shutil.copy(os.path.join(ROOT, "HASIL_LONG_SHORT.txt"), os.path.join(dl, "HASIL_LONG_SHORT.txt"))
    print("HASIL_LONG_SHORT.txt disalin ke folder Download.")
print("SELESAI. File hasil:", os.path.join(ROOT, "HASIL_LONG_SHORT.txt"))
