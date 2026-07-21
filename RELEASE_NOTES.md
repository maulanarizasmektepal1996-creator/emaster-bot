# Versi 21.3.0 - Direktori Jabatan Lokal

- Menggunakan **DATA PEGAWAI BID PEMASARAN(1).xlsx** tab `update` sebagai satu-satunya sumber nama dan jabatan profil.
- Mengimpor 23 profil yang memiliki NIP/NPPK valid dan melewati baris tanpa identitas agar tidak salah memasangkan pegawai.
- Mencocokkan profil berdasarkan NIP/NPPK yang dinormalisasi, bukan nama atau urutan baris.
- Menyimpan hanya sidik SHA‑256 NIP/NPPK di paket; nomor mentah, nomor telepon, email, pangkat, dan data tab lama tidak disertakan.
- Sinkronisasi profil berlangsung lokal dan instan sehingga tidak memperlambat login OTP atau bergantung pada Google Sheet.
- `/start`, menu utama, login sukses, dan tombol Perbarui Profil menggunakan direktori yang sama.
- Kegiatan Tugas Jabatan untuk pengisian aktivitas tetap diambil dinamis dari e‑Master milik masing-masing pegawai.
- Mempertahankan database, akun pegawai, enkripsi, sesi, aktivitas, draf, dan seluruh fitur versi 21.2.0.

# Versi 21.2.0 - Tanggal Satu Bulan Penuh

- Mengizinkan aktivitas pada seluruh tanggal bulan berjalan, dari tanggal 1 sampai hari terakhir.
- Tanggal mendatang dalam bulan yang sama sekarang diperbolehkan.
- Tanggal sebelumnya tidak lagi dibatasi H+7 selama masih berada pada bulan berjalan.
- Aturan yang sama berlaku untuk Tambah, Edit, Salin Aktivitas, dan Lanjutkan Draft.
- Tanggal di luar bulan berjalan tetap ditolak untuk mencegah salah periode.
- Pilihan tanggal sekarang menampilkan rentang bulan aktif secara jelas.

# Versi 21.1.0 - Profil Pegawai di Halaman Depan

- Menampilkan nama pegawai, NIP, dan jabatan pada kartu halaman depan setelah login.
- Mengambil profil langsung dari halaman Info Jabatan akun e‑Master setelah OTP berhasil.
- Menyimpan cache profil secara terpisah untuk setiap Telegram ID agar menu berikutnya terbuka cepat.
- Menambahkan tombol Perbarui Profil untuk menyinkronkan perubahan jabatan dari e‑Master.
- NIP lengkap hanya ditampilkan di percakapan pribadi; pada konteks selain chat pribadi tetap disamarkan.
- Menambahkan migrasi otomatis kolom profil untuk database versi lama.
- Kegagalan membaca profil tidak membatalkan login atau fungsi aktivitas harian.

# Versi 21.0.1 - Hotfix Database Favorit Lama

- Memperbaiki gangguan setelah tugas jabatan dipilih pada database yang pernah dipakai versi single-user.
- Menghindari benturan dengan skema tabel `favorites` lama melalui tabel favorit multipegawai khusus.
- Memigrasikan favorit lama kepada akun admin secara otomatis tanpa mengubah atau menghapus tabel sumber.
- Menambahkan fallback sehingga kegagalan membaca favorit tidak dapat menghentikan pencarian Kamus Aktivitas.
- Menambahkan pengujian upgrade nyata dari struktur database favorit versi lama.

# Versi 21.0.0 - Smart Activity & UI

- Menambahkan Edit Aktivitas langsung melalui form resmi e‑Master untuk tanggal, kamus, volume, dan objek kerja.
- Perubahan terikat ke ID realisasi yang dipilih, divalidasi endpoint-nya, dan diverifikasi ulang setelah disimpan.
- Menambahkan Salin Aktivitas dari riwayat dengan pencocokan tugas jabatan pegawai dan WPT kamus terbaru.
- Menghitung total WPT live seluruh tugas pada tanggal yang sama dan memblokir total di atas 660 menit.
- Mendeteksi aktivitas duplikat dan meminta persetujuan eksplisit sebelum tetap mengirim.
- Menambahkan Favorit Saya yang terpisah untuk setiap pegawai.
- Menambahkan draf persisten per pegawai dan tombol Lanjutkan Draft setelah OTP/sesi habis.
- Memperbarui menu utama, ringkasan konfirmasi, riwayat, serta pesan sukses agar lebih ringkas dan konsisten.
- Menambahkan fallback untuk tombol kedaluwarsa sehingga setiap klik selalu mendapat respons.
- Menambahkan audit edit lokal tanpa menyimpan password, OTP, atau cookie.
- Menambah cakupan pengujian konektor edit, isolasi data pegawai, aksi riwayat, dan pemeriksaan WPT.

# Versi 20.0.0 - Hapus Aktivitas Aman

- Menu Riwayat sekarang membaca data terbaru langsung dari e-Master.
- Seluruh aktivitas bulan berjalan dapat dijelajahi lima item per halaman.
- Aktivitas yang dibuat lewat bot maupun situs e-Master dapat ditemukan.
- Setiap baris mempunyai tombol Hapus.
- Penghapusan memakai konfirmasi dua langkah dan menampilkan tanggal, aktivitas, objek kerja, volume, serta WPT.
- Bot memvalidasi domain, endpoint, bulan, breakdown, dan ID realisasi sebelum mengirim permintaan hapus.
- Bot memeriksa ulang halaman e-Master dan baru menyatakan berhasil jika aktivitas sudah benar-benar hilang.
- Konfirmasi terikat pada ID aktivitas agar tombol lama tidak dapat menghapus item yang berbeda.
- Penghapusan dicatat dalam tabel audit lokal tanpa menyimpan password, OTP, atau cookie.

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
