# Bot Telegram Aktivitas e‑Master (Multipegawai)

Bot admin-terkelola untuk mencari Kamus Aktivitas Disbudpar, menghitung WPT, meminta konfirmasi, lalu mengirim aktivitas ke akun e‑Master masing-masing pegawai.

## Fitur

- Admin mendaftarkan pegawai berdasarkan Telegram ID dan NIP.
- Password dimasukkan sendiri oleh pegawai, langsung dihapus dari chat, lalu disimpan terenkripsi.
- Akun, cookie sesi, dashboard WPT, dan riwayat setiap pegawai terpisah.
- Admin dapat melihat status dan menonaktifkan pegawai.
- Login NIP/password dan OTP Google Authenticator saat sesi berakhir.
- Setiap pegawai yang menjalankan `/login` selalu memulai sesi baru dan wajib memasukkan OTP miliknya.
- Cookie sesi disimpan terenkripsi.
- Pencarian Kamus Aktivitas lengkap dari e‑Master dengan jaring pengaman 72 aktivitas terverifikasi dari PDF BKD terbaru.
- Pagination hasil pencarian: semua hasil dapat dibuka tanpa batas delapan tombol.
- Mengambil semua Kegiatan Tugas Jabatan secara otomatis dan menampilkannya sebagai pilihan.
- Satuan dan WPT otomatis mengikuti kamus.
- Validasi H+7 dan maksimum 660 menit per aktivitas yang dikirim.
- Konfirmasi sebelum data dikirim.
- Progres mengambil total WPT terbaru langsung dari e‑Master, termasuk aktivitas yang diinput melalui situs.
- Menu utama interaktif dengan tombol.
- Pilihan tanggal cepat Hari Ini dan Kemarin.
- Dashboard visual dengan progress bar, kekurangan WPT, dan estimasi hari.
- Riwayat aktivitas yang dikirim melalui bot.
- Tombol Tambah Lagi setelah aktivitas berhasil disimpan.

## Perintah

- `/start` — membuka menu utama.
- `/login` — masuk atau memeriksa sesi e‑Master.
- `/tambah` — menambahkan aktivitas.
- `/dashboard` atau `/progres` — melihat WPT terbaru.
- `/riwayat` — melihat aktivitas terakhir dari bot.
- `/batal` — menghentikan proses pengisian.
- `/tambahpegawai` — admin mendaftarkan pegawai.
- `/aktifkan` — pegawai melengkapi password setelah diundang admin.

## Menambahkan pegawai

1. Pegawai membuka bot dan menjalankan `/start` untuk melihat Telegram ID.
2. Admin memilih **Kelola Pegawai → Tambah Pegawai** atau menjalankan `/tambahpegawai`.
3. Admin memasukkan Telegram ID, NIP, dan nama pegawai.
4. Pegawai menjalankan `/aktifkan`, lalu mengirim password e‑Master sendiri.
5. Pesan password otomatis dihapus dan disimpan menggunakan enkripsi Fernet.
6. Pegawai menjalankan `/login` dan memasukkan OTP Google Authenticator miliknya.

Jangan membagikan akun Telegram. Admin tidak dapat melihat password asli pegawai melalui menu bot.

## Keamanan

- Gunakan repository GitHub privat dan batasi akses proyek Railway.
- `ENCRYPTION_KEY` harus dibuat sekali, disimpan sebagai Railway Variable, dan tidak dibagikan.
- Password serta cookie sesi disimpan menggunakan enkripsi Fernet; pesan password dan OTP dihapus setelah diterima bot.
- Database dan file sesi menggunakan izin file privat pada container.
- Sesi, tugas jabatan, dashboard, dan riwayat dipisahkan berdasarkan Telegram ID.
- Menonaktifkan atau mendaftarkan ulang pegawai membersihkan cache dan sesi lamanya.
- Jangan menyalakan log debug atau membagikan Railway Logs yang mungkin memuat konteks teknis.

## Catatan penting

Ini konektor tidak resmi dan hanya boleh dipakai untuk akun sendiri dengan izin instansi. Perubahan halaman e‑Master dapat membuat login atau pengiriman perlu diperbarui. Selalu periksa aktivitas yang masuk sebelum laporan bulanan dikunci/dikirim.

## Kamus aktivitas

File `kamus_aktivitas.json` memuat 72 aktivitas dari **KAMUS KINERJA BIDANG PEMASARAN DAN KELEMBAGAAN PAREKRAF - KAMUS BKD**, versi 18 Juli 2026. Bot menggabungkannya dengan hasil live e‑Master agar item tidak hilang akibat batas tampilan atau pagination popup.

Pencarian dilakukan pada nama aktivitas. Contoh `surat` menghasilkan 10 aktivitas (kode 19, 20, 21, 27, 28, 40, 50, 51, 59, dan 65). Bot menampilkan delapan item per halaman; tekan **Berikutnya** untuk melihat sisanya.

Untuk memperbarui kamus di kemudian hari, ganti isi `kamus_aktivitas.json` dengan daftar yang sudah disahkan dan pertahankan struktur `code`, `activity`, `unit`, `wpt`, serta `object_hint`.

## Menyiapkan bot Telegram

1. Buka `@BotFather` di Telegram.
2. Jalankan `/newbot`, lalu simpan tokennya.
3. Dapatkan Telegram User ID Anda dari `@userinfobot`.
4. Jangan kirim token, password, OTP, atau kunci enkripsi melalui chat.

## Membuat kunci enkripsi

Jalankan sekali di komputer:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`ENCRYPTION_KEY` dibuat satu kali dan jangan diganti setiap deploy. Jika sesi
e‑Master kedaluwarsa, bot akan membersihkan cookie lama secara otomatis saat
`/login` dijalankan.

## Deploy Railway

1. Buat repository GitHub baru dan unggah seluruh isi folder ini.
2. Railway → **New Project** → **Deploy from GitHub Repo**.
3. Tambahkan Railway Volume dan mount ke `/data`.
4. Buka **Variables**, lalu isi semua variabel pada `.env.example`.
5. Railway akan menjalankan worker menggunakan `python main.py`.
6. Buka Telegram: `/start`, kemudian `/login`.
7. Saat diminta, kirim OTP 6 digit. Pesan OTP akan berusaha dihapus segera setelah diproses.
8. Jalankan `/tambah` untuk uji satu aktivitas.

## Memperbarui dari versi sebelumnya

1. Simpan nilai Railway Variables dan pastikan Volume `/data` tetap terpasang.
2. Ganti kode proyek dengan isi paket versi terbaru, lalu deploy ulang.
3. **Jangan membuat atau mengganti `ENCRYPTION_KEY`.** Kunci lama diperlukan untuk membuka password pegawai yang sudah tersimpan.
4. Tidak perlu mengisi ulang `EMASTER_BREAKDOWN_ID`, `EMASTER_TARGET_ID`, atau hash tugas; versi ini mengambil tugas jabatan setiap pegawai langsung dari e‑Master.
5. Setelah deploy, setiap pegawai menjalankan `/login` dan memasukkan OTP baru.
6. Uji `/tambah`, cari `surat`, dan pastikan tertulis **10 hasil** dengan tombol **Berikutnya** untuk halaman kedua.

## Menjalankan lokal

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Untuk lokal, ubah `DATABASE_PATH=./data/emaster_bot.db` dan `SESSION_PATH=./data/emaster_session.bin`.

## Pemeriksaan pertama

Gunakan satu aktivitas uji yang benar, tekan konfirmasi, lalu buka e‑Master dan pastikan tanggal, aktivitas, WPT, volume, dan objek kerja sama persis. Jika login gagal karena struktur form berubah, jangan mencoba OTP berulang kali; lihat log tanpa mengaktifkan debug yang dapat merekam data sensitif.
