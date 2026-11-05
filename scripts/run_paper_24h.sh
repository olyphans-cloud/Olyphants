#!/usr/bin/env bash
# Paper trading 24 jam: jalankan paper-step tiap candle 15m selesai.
# Jalankan dari folder olypahantstrade/ (sejajar config.mtf.example.json).
# Butuh internet. Hanya trade VIRTUAL, tidak ada uang asli.
#
# Pakai:  bash scripts/run_paper_24h.sh
# Di Termux, supaya tidak mati saat layar off:  termux-wake-lock

CONFIG="config.mtf.example.json"
LOG="paper_24h.log"
ROUNDS=96            # 96 x 15 menit = 24 jam

echo "Mulai: $(date -u +%FT%TZ)" | tee -a "$LOG"

for i in $(seq 1 "$ROUNDS"); do
  # tunggu sampai 15 detik setelah batas 15 menit (candle sudah tutup)
  now=$(date +%s)
  next=$(( (now / 900 + 1) * 900 + 15 ))
  sleep $(( next - now ))

  echo "=== ronde $i/$ROUNDS $(date -u +%FT%TZ) ===" | tee -a "$LOG"
  python3 -m btc_robot --config "$CONFIG" paper-step 2>&1 | tee -a "$LOG"
done

echo "Selesai: $(date -u +%FT%TZ)" | tee -a "$LOG"
python3 -m btc_robot halt-status 2>&1 | tee -a "$LOG"
echo "Kirim paper_24h.log dan folder state/ ke Claude untuk dianalisis."
