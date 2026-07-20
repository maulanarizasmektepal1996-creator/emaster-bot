# Versi 19.0.0 - Kamus Lengkap

- Memuat 72 aktivitas terverifikasi dari PDF Kamus BKD versi 18 Juli 2026.
- Menghapus batas delapan hasil pada parser popup e-Master.
- Menambahkan pagination Telegram delapan item per halaman.
- Kata kunci `surat` tervalidasi menghasilkan tepat 10 aktivitas.
- Menambahkan tombol ganti kata kunci tanpa mengulang seluruh formulir.
- Menangani tombol hasil yang sudah kedaluwarsa tanpa membuat bot macet.
- Data live e-Master tetap dapat memperbarui satuan/WPT untuk kode yang sama.
- Login, OTP per pegawai, tugas jabatan dinamis, enkripsi, dan database versi sebelumnya tetap dipertahankan.
- Dependensi production/stable diperbarui dan dipin agar deploy dapat direproduksi.

Saat upgrade, pertahankan Railway Volume `/data` dan nilai `ENCRYPTION_KEY` lama.
