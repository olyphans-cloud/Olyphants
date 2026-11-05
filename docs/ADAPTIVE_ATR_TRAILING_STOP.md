# Adaptive ATR Trailing Stop - Olyphants

Status: PROPOSED — wajib melalui backtest dan paper trading.
Tidak boleh langsung diaktifkan pada akun LIVE.

## 1. Tujuan

Membiarkan posisi mengikuti tren jangka panjang sambil mengamankan
keuntungan secara bertahap. Stop-loss tidak boleh bergerak mundur.

## 2. Parameter awal pengujian

- Timeframe trailing: H4
- ATR period: 14
- ATR multiplier: 2.0
- Initial stop-loss: 2% dari harga entry
- Aktivasi trailing: setelah pergerakan harga menguntungkan mencapai 5%
- TP1: +5% dari entry
- TP2: +10% dari entry
- TP3: +15% dari entry
- Pembagian penutupan awal: 30%, 30%, 20%
- Sisa 20% dikelola dengan trailing stop

Semua persentase TP dan SL adalah perubahan harga aset,
bukan ROI margin atau persentase modal. Biaya, funding,
spread, dan slippage harus diperhitungkan terpisah.

## 3. Aturan LONG

Initial SL:

    initial_sl = entry_price * (1 - 0.02)

Simpan highest_price sejak entry.

Setelah kenaikan harga mencapai 5%, aktifkan trailing:

    candidate_sl = highest_price - (2.0 * ATR_H4)

Stop baru:

    new_sl = max(previous_sl, candidate_sl)

SL hanya boleh naik atau tetap. SL tidak boleh turun.

Jika candidate_sl belum lebih tinggi daripada previous_sl,
pertahankan previous_sl.

## 4. Aturan SHORT

Initial SL:

    initial_sl = entry_price * (1 + 0.02)

Simpan lowest_price sejak entry.

Setelah penurunan harga mencapai 5%, aktifkan trailing:

    candidate_sl = lowest_price + (2.0 * ATR_H4)

Stop baru:

    new_sl = min(previous_sl, candidate_sl)

SL hanya boleh turun atau tetap. SL tidak boleh naik.

Jika candidate_sl belum lebih rendah daripada previous_sl,
pertahankan previous_sl.

## 5. TP bertahap

TP dihitung dari harga entry, bukan dari leverage.

LONG:
- TP1 ketika harga naik 5%.
- TP2 ketika harga naik 10%.
- TP3 ketika harga naik 15%.

SHORT:
- TP1 ketika harga turun 5%.
- TP2 ketika harga turun 10%.
- TP3 ketika harga turun 15%.

Pembagian awal penutupan posisi:
- TP1: 30%
- TP2: 30%
- TP3: 20%
- Sisa 20%: trailing stop

Setelah partial close, kuantitas stop-loss wajib disesuaikan
dengan sisa posisi. Jangan sampai stop menutup posisi lebih
besar daripada kuantitas yang masih terbuka.

## 6. Invariant keselamatan

1. LONG: stop tidak pernah turun.
2. SHORT: stop tidak pernah naik.
3. Stop tidak boleh hilang ketika order diperbarui.
4. Verifikasi stop aktif setelah entry dan setiap perubahan.
5. Gunakan proteksi close-only/reduce-only pada Futures.
6. Hindari order penutupan ganda.
7. Tangani partial fill, slippage, biaya, dan kegagalan API.
8. Jika pembaruan stop gagal, jalankan prosedur emergency
   sesuai status posisi; jangan menganggap stop sudah aktif.
9. Setelah restart, pulihkan entry, high/low watermark,
   stop terakhir, dan kuantitas posisi dari state persisten
   serta verifikasi dengan posisi/order di bursa.
10. Spot dan Futures memakai invariant satu arah yang sama,
    tetapi adapter dan mekanisme order harus terpisah.

## 7. Catatan ATR

ATR mengukur volatilitas, bukan arah harga.

ATR harus dihitung dari candle H4 yang sudah tertutup agar
nilai trailing tidak berubah-ubah selama candle berjalan.

ATR multiplier 2.0 adalah parameter awal pengujian,
bukan angka optimal yang sudah terbukti.

Jika ATR berubah dan candidate stop lebih buruk daripada
stop sebelumnya, invariant satu arah tetap berlaku.

## 8. Pengujian wajib sebelum LIVE

- Unit test monotonic stop LONG dan SHORT.
- Test aktivasi trailing pada ambang 5%.
- Test TP partial close dan sisa kuantitas.
- Test gap harga dan slippage.
- Test restart dan pemulihan state.
- Test API timeout, reject, duplicate order, dan partial fill.
- Backtest dengan fee, funding, spread, dan slippage.
- Walk-forward dan paper trading multi-hari.

Jangan aktifkan LIVE sebelum seluruh pengujian dan audit
eksekusi dinyatakan lulus.
