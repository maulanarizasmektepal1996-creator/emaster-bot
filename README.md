# Bot Telegram Aktivitas e‑Master (Mode Pribadi)

Bot pribadi untuk mencari Kamus Aktivitas Disbudpar, menghitung WPT, meminta konfirmasi, lalu mengirim aktivitas ke e‑Master BKD Jawa Timur.

## Fitur

- Hanya menerima perintah dari satu `TELEGRAM_USER_ID`.
- Login NIP/password dan OTP Google Authenticator saat sesi berakhir.
- Cookie sesi disimpan terenkripsi.
- Pencarian Kamus Aktivitas langsung dari e‑Master.
- Mengambil semua Kegiatan Tugas Jabatan secara otomatis dan menampilkannya sebagai pilihan.
- Satuan dan WPT otomatis mengikuti kamus.
- Validasi H+7 dan maksimum 660 menit per aktivitas yang dikirim.
- Konfirmasi sebelum data dikirim.
- Progres mengambil total WPT terbaru langsung dari e‑Master, termasuk aktivitas yang diinput melalui situs.

## Catatan penting

Ini konektor tidak resmi dan hanya boleh dipakai untuk akun sendiri dengan izin instansi. Perubahan halaman e‑Master dapat membuat login atau pengiriman perlu diperbarui. Selalu periksa aktivitas yang masuk sebelum laporan bulanan dikunci/dikirim.

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
