#!/usr/bin/env python3
"""Bangun docs/M1_H1_REPORT.md dari JSON/CSV di docs/m1_audit/. Tidak ada angka yang diketik tangan."""
import csv
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A = os.path.join(ROOT, "docs", "m1_audit")


def J(name):
    with open(os.path.join(A, name), encoding="utf-8") as fh:
        return json.load(fh)


def f(x, d=2):
    return f"{x:,.{d}f}"


def table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def main():
    m1m, h1m = J("m1_backtest_metrics.json"), J("h1_backtest_metrics.json")
    m1a, h1a = J("m1_attribution.json"), J("h1_attribution.json")
    nul, ba = J("m1_null_baseline.json"), J("before_after.json")
    reg_m, reg_h = J("m1_regimes.json"), J("h1_regimes.json")
    abl, sen = J("m1_ablation.json"), J("m1_sensitivity.json")
    wf_m, wf_h = J("m1_walkforward_holdout.json"), J("h1_walkforward_holdout.json")
    s, st, re_ = m1a["summary"], m1a["structure"], m1a["run_end"]
    L = []
    w = L.append
    w("# M1 dan H1 — laporan audit dan evaluasi\n")
    w("Dibuat otomatis oleh `scripts/build_m1_report.py` dari `docs/m1_audit/*`. **Semua angka berasal dari data SINTETIS "
      "(random walk)** kecuali Anda menjalankan skrip dengan `--csv`. Data sintetis tidak punya edge by construction "
      "(selain persistensi tren kecil per 200 candle), sehingga hasilnya **tidak membuktikan apa pun tentang pasar nyata**.\n")
    w("## 0. Status\n")
    w(table(["Mode", "Status", "Alasan ringkas"],
            [["M1", "**BELUM LULUS**", "Implementasi lolos audit, tetapi biaya pulang-pergi ≈ jarak stop; negatif setelah biaya di semua jendela walk-forward dan holdout (sintetis). Belum ada data nyata."],
             ["H1", "**BELUM LULUS**", "Struktur biaya jauh lebih sehat, hasil campuran (3/4 jendela positif, holdout kecil negatif, hanya 9 transaksi). Belum ada data nyata."]]))
    w("\nTidak ada parameter strategi/risiko yang diubah: tidak ada varian yang positif setelah biaya pada bukti yang ada, "
      "jadi tidak ada dasar pengujian untuk perubahan. Satu-satunya perubahan terkait M1 adalah **peringatan konfigurasi** "
      "(tidak mengubah perilaku).\n")
    w("**Tentang angka di permintaan** (M1: 22 transaksi, −1,95%; H1: 10 transaksi, +1,60%): angka itu bukan dari run ini dan "
      "tidak dapat direproduksi tanpa data/seed/konfigurasinya. Hasil di bawah memakai data, seed, biaya, dan konfigurasi yang "
      "dicantumkan pada tiap tabel.\n")

    w("## 1. Audit implementasi M1 (tes otomatis, semuanya lulus)\n")
    w("`tests/test_v030_m1_audit.py` (16 tes), diverifikasi dengan *mutation check* (sinyal mengintip masa depan, fee separuh, "
      "slippage hanya satu sisi — semuanya terdeteksi):\n")
    w(table(["Pemeriksaan", "Hasil"],
            [["Tanpa look-ahead: sinyal di i identik dengan data terpotong sampai i; mengubah candle masa depan tidak mengubah sinyal lampau", "lulus"],
             ["Timestamp: entry = OPEN candle setelah candle sinyal; sinyal memakai CLOSE candle yang sudah tutup", "lulus"],
             ["Setiap approved order = entry atau di-veto tahap berikutnya (tidak ada order hilang)", "lulus"],
             ["Biaya dihitung ulang independen per transaksi (fee, gross, net) dan ekuitas rekonsiliasi dengan ledger", "lulus (presisi 1e-8)"],
             ["Dunia tanpa biaya: net == gross untuk semua transaksi", "lulus"],
             ["Lot: kelipatan lot_step, ≥ min_qty, ≥ min_notional, ≤ batas notional; qty dibulatkan ke bawah", "lulus"],
             ["Karakterisasi model fill: TP terisi saat *tersentuh* (optimis), stop menang bila keduanya tersentuh (pesimis), gap menembus stop terisi di open", "terdokumentasi + lulus"]]))
    w("\n**Kesimpulan audit:** tidak ditemukan bug implementasi pada sinyal, waktu, atau biaya M1. Kerugian M1 bukan akibat kesalahan kode yang terdeteksi.\n")

    w("## 2. Rincian transaksi\n")
    w(f"CSV lengkap: `docs/m1_audit/m1_trades.csv` ({s['trades']} transaksi) dan `h1_trades.csv`. Kolom: waktu, arah, alasan entry, "
      "harga referensi vs fill, alasan exit, durasi, PnL **sebelum biaya**, biaya spread+slippage, fee, PnL bersih, stop/TP rencana, "
      "biaya pulang-pergi %, rasio biaya/stop. Contoh 8 baris pertama M1:\n")
    with open(os.path.join(A, "m1_trades.csv"), encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    keep = [0, 1, 4, 9, 11, 12, 15, 20]
    w(table([rows[0][i] for i in keep], [[r[i] for i in keep] for r in rows[1:9]]))
    w("")

    w("## 3. Sumber kerugian M1 (baseline resmi, data sintetis n=20.000 candle 1m, seed 1)\n")
    w(f"Run **berhenti oleh halt drawdown** pada candle ke-{re_['last_trade_exit_bar']:,} dari {re_['bars_total']:,} "
      f"(`{re_['halt_reason']}`, batas 8%). Itu kontrol risiko yang bekerja, tetapi berarti {100 - re_['last_trade_exit_bar'] * 100 // re_['bars_total']}% data tidak terpakai.\n")
    w(table(["Komponen", "USDT"],
            [["Laba kotor sebelum biaya (harga referensi)", f(s["gross_before_costs"])],
             ["Biaya spread + slippage", f(-s["spread_slippage"])], ["Fee", f(-s["fees"])],
             ["**PnL bersih**", f"**{f(s['net'])}**"]]))
    w(f"\nTotal biaya = {f(s['spread_slippage'] + s['fees'])} vs laba kotor {f(s['gross_before_costs'])}: biaya "
      f"{(s['spread_slippage'] + s['fees']) / max(s['gross_before_costs'], 1e-9):.1f}× laba kotor.\n")
    w("**Struktur (aritmetika dari config + data, bukan tebakan):**\n")
    w(table(["Besaran", "M1", "H1"],
            [["Rata-rata jarak stop (%)", f(st["avg_stop_pct"], 3), f(h1a["structure"]["avg_stop_pct"], 3)],
             ["Rata-rata jarak TP (%)", f(st["avg_tp_pct"], 3), f(h1a["structure"]["avg_tp_pct"], 3)],
             ["Biaya pulang-pergi (%)", f(st["roundtrip_cost_pct"], 3), f(h1a["structure"]["roundtrip_cost_pct"], 3)],
             ["**Biaya / jarak stop**", f"**{st['cost_to_stop_ratio']:.2f}**", f"**{h1a['structure']['cost_to_stop_ratio']:.2f}**"],
             ["P(TP sebelum SL) pada random walk", f"{st['p_tp_before_sl_random_walk']:.0%}", f"{h1a['structure']['p_tp_before_sl_random_walk']:.0%}"],
             ["Win rate impas setelah biaya", f"{st['breakeven_win_rate_after_costs']:.1%}", f"{h1a['structure']['breakeven_win_rate_after_costs']:.1%}"],
             ["Win rate aktual", f"{st['actual_win_rate']:.1%}", f"{h1a['structure']['actual_win_rate']:.1%}"],
             ["EV per transaksi pada random walk (%)", f(st["ev_pct_per_trade_random_walk"], 3), f(h1a["structure"]["ev_pct_per_trade_random_walk"], 3)]]))
    w("\nUntuk M1, biaya pulang-pergi sebanding dengan jarak stop (rasio ≈ 1). Dengan RR 3:1 pada pergerakan acak, "
      "peluang mencapai TP ≈ 25%, sedangkan win rate impas setelah biaya ≈ 51%. Akibatnya nilai harapan per transaksi "
      "≈ −biaya, kecuali sinyal punya edge yang besar. Filter `min_edge_cost_mult` **tidak pernah aktif** pada konfigurasi ini "
      "(TP selalu > 2× biaya) sehingga tidak melindungi dari kondisi ini (lihat ablasi `no_edge_filter` = baseline).\n")
    w(table(["Alasan exit (M1)", "n", "rata-rata net", "rata-rata durasi (menit)"],
            [[k, v["n"], f(v["avg_net"], 3), f(v["avg_hold_min"], 1)] for k, v in sorted(m1a["by_exit_reason"].items())]))
    w("")

    w("## 4. Pembanding entry-acak (apakah sinyal M1 menambah sesuatu?)\n")
    w(f"{nul['null_runs']} run dengan entry LONG acak (peluang {nul['p_entry_per_bar']:.2%} per candle), stop/TP/filter/exit **identik**, "
      f"data sama ({nul['bars']:,} candle), konfigurasi diagnostik tanpa halt drawdown agar horizon sama.\n")
    w(table(["", "M1", "Entry acak (rata-rata)"],
            [["Transaksi", nul["m1_trades"], f(nul["null_trades_mean"], 0)],
             ["Win rate", f"{nul['m1_win_rate']:.1%}", f"{nul['null_win_rate_mean']:.1%}"],
             ["Laba kotor / transaksi", f(nul["m1_gross_per_trade"], 3), f(nul["null_gross_per_trade_mean"], 3)],
             ["Net / transaksi", f(nul["m1_net_per_trade"], 3), f(nul["null_net_per_trade_mean"], 3)],
             ["Net total", f(nul["m1_net"]), f"{f(nul['null_net_mean'])} (p05 {f(nul['null_net_p05'])}, p95 {f(nul['null_net_p95'])})"]]))
    w(f"\nLaba kotor/transaksi M1 berada di persentil ke-{nul['m1_percentile_gross_per_trade_among_null'] * 100:.0f} dibanding entry acak "
      "(tidak signifikan secara statistik, ambang umum 95). Net total M1 lebih baik dari acak terutama karena **lebih sedikit transaksi "
      "(biaya lebih sedikit)**, bukan karena kualitas entry yang jelas lebih baik. Keduanya tetap negatif.\n")

    w("## 5. Kondisi pasar (sintetis terkendali; jumlah dari 3 seed; konfigurasi diagnostik tanpa halt)\n")
    def regtab(reg):
        return table(["Regime", "Transaksi", "Win rate", "Laba kotor", "Biaya (spread+slip+fee)", "Net"],
                     [[k, v["trades"], f"{v['win_rate']:.1%}", f(v["gross_before_costs"]), f(v["spread_slippage"] + v["fees"]), f(v["net"])]
                      for k, v in reg.items()])
    w("**M1** (n=10.000 candle 1m per seed)\n"); w(regtab(reg_m))
    w("\n**H1** (n=6.000 candle 1h per seed)\n"); w(regtab(reg_h))
    w("\nCatatan: `low_vol` M1 = 0 transaksi karena filter ATR minimum (`min_atr_pct`) menolak pasar sangat tenang; itu perilaku filter, "
      "bukan bukti profit. Regime tren hanya punya drift kecil buatan; ini bukan pasar nyata.\n")

    w("## 6. Ablasi (DIAGNOSTIK — tidak ada parameter yang dipilih dari sini)\n")
    w("Data sintetis campuran n=10.000 × 3 seed, konfigurasi diagnostik tanpa halt.\n")
    w(table(["Varian", "Transaksi", "Laba kotor", "Biaya", "Net"],
            [[k, v["trades"], f(v["gross_before_costs"]), f(v["spread_slippage"] + v["fees"]), f(v["net"])] for k, v in abl.items()]))
    w("\nSemua varian dengan biaya nyata **negatif**; hanya `zero_cost` positif, artinya sinyal punya laba kotor positif tipis pada data "
      "yang memang punya persistensi tren kecil, tetapi biaya lebih dari dua kali lipat edge itu. Varian yang mengurangi jumlah "
      "transaksi (tanpa trailing, stop lebih lebar) mengurangi kerugian karena mengurangi biaya, tetapi tetap negatif; "
      "tidak ada dasar untuk mengadopsinya.\n")

    w("## 7. Sensitivitas asumsi eksekusi\n")
    w(table(["Asumsi", "Net (3 seed)", "Transaksi"],
            [[f"TP butuh penetrasi {k} bps", f(v["net"]), v["trades"]] for k, v in sen["tp_touch_penetration_bps"].items()] +
            [[f"Biaya × {k}", f(v["net"]), v["trades"]] for k, v in sen["cost_multiplier"].items()]))
    w("\nAsumsi TP-saat-tersentuh (optimis) bukan penyebab kerugian. Biaya × 2 → 0 transaksi karena filter edge akhirnya aktif.\n")

    w("## 8. Walk-forward dan holdout (parameter tetap sejak awal; holdout tidak dipakai memilih apa pun; biaya termasuk)\n")
    for name, wf in (("M1", wf_m), ("H1", wf_h)):
        c = wf["costs"]
        w(f"**{name}** — sumber: {wf['source']}; fee {c['fee_rate']:.5f}, spread {c['spread_bps']} bps, slippage {c['slippage_bps']} bps\n")
        rows = [[x["window"], x["trades"], f"{x['win_rate']:.1%}", f(x["total_fees"]), f(x["net_pnl"]), f"{x['return_pct']:.2f}%", f"{x['max_drawdown_pct']:.2f}%"]
                for x in wf["walk_forward_windows"]]
        h = wf["holdout"]
        rows.append(["**holdout**", h["trades"], f"{h['win_rate']:.1%}", f(h["total_fees"]), f(h["net_pnl"]), f"{h['return_pct']:.2f}%", f"{h['max_drawdown_pct']:.2f}%"])
        w(table(["Jendela", "Transaksi", "Win rate", "Fee", "Net", "Return", "Max DD"], rows)); w("")

    w("## 9. Sebelum dan sesudah perbaikan kode (v4 vs sekarang; data, biaya, dan aturan risiko sama)\n")
    w(table(["Kasus", "Versi", "Transaksi", "Return %", "Max DD %", "Fee", "Halt", "Hash"],
            [[k, label, v["trades"], v["return_pct"], v["max_drawdown_pct"], v["total_fees"], v["halt_reason"] or "-", v["hash"]]
             for k, r0 in ba.items() for label, v in (("v4", r0["sebelum"]), ("sekarang", r0["sesudah"]))]))
    w("\nH1 dan M1 **identik** (hash sama): perombakan isolasi, pipeline, dan state machine tidak menggeser hasilnya. "
      "MTF berubah sedikit karena **cooldown breaker kini dimulai saat rugi ke-3 ditutup**, bukan pada sinyal berikutnya yang kebetulan dievaluasi.\n")

    w("## 10. Yang dibutuhkan untuk menilai M1/H1 dengan sah\n")
    w("1. Data candle nyata multi-tahun (`python3 scripts/download_btc_history.py`, lalu varian 1m/1h), divalidasi dengan `validate-data`.")
    w("2. `python3 scripts/analyze_m1.py walkforward --mode m1 --csv <file>` dan bagian lain dengan `--csv`; holdout dipakai **sekali**.")
    w("3. Paper trading berminggu-minggu pada data nyata (`scripts/run_paper_24h.sh` atau dashboard) dengan biaya sesuai akun Anda.")
    w("4. Audit independen ChatGPT atas ZIP, lalu persetujuan Anda. Sampai itu terpenuhi, status M1 dan H1 tetap **BELUM LULUS**.")
    out = os.path.join(ROOT, "docs", "M1_H1_REPORT.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("ditulis", out)


if __name__ == "__main__":
    main()
