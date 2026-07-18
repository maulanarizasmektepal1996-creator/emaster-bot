from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler, filters)

from emaster import AuthenticationRequired, EMasterClient, EMasterError, KamusItem, WorkTarget
from storage import Storage

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

OWNER = int(os.environ["TELEGRAM_USER_ID"])
storage = Storage(os.getenv("DATABASE_PATH", "/data/emaster_bot.db"))
fernet = Fernet(os.environ["ENCRYPTION_KEY"].encode())
storage.ensure_admin(OWNER, os.environ["EMASTER_NIP"],
                     fernet.encrypt(os.environ["EMASTER_PASSWORD"].encode()).decode())
storage.claim_legacy_activities(OWNER)
clients: dict[int, EMasterClient] = {}
session_dir = Path(os.getenv("SESSION_PATH", "/data/emaster_session.bin")).parent / "sessions"


def get_client(telegram_id: int) -> EMasterClient:
    user = storage.get_user(telegram_id)
    if not user or user[4] != "active" or not user[2]:
        raise EMasterError("Akun belum aktif.")
    if telegram_id not in clients:
        password = fernet.decrypt(user[2].encode()).decode()
        session_path = os.getenv("SESSION_PATH", "/data/emaster_session.bin") if telegram_id == OWNER else str(session_dir / f"{telegram_id}.bin")
        clients[telegram_id] = EMasterClient(user[1], password, os.environ["ENCRYPTION_KEY"],
                                             session_path)
    return clients[telegram_id]

DATE, TARGET, SEARCH, PICK, VOLUME, OBJECT, CONFIRM, OTP, ADMIN_TGID, ADMIN_NIP, ADMIN_NAME, ACTIVATE_PASSWORD = range(12)


def private(fn):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = storage.get_user(update.effective_user.id) if update.effective_user else None
        if not user or user[4] != "active":
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Akun belum aktif. Minta admin mendaftarkan Telegram ID Anda, lalu gunakan /aktifkan.")
            return ConversationHandler.END
        return await fn(update, context)
    return wrapped


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = storage.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text(f"👋 Telegram ID Anda: `{update.effective_user.id}`\n\nMinta admin mendaftarkan ID ini.", parse_mode="Markdown")
    elif user[4] == "invited":
        await update.message.reply_text("✅ Anda sudah didaftarkan admin. Jalankan /aktifkan untuk membuat akses pribadi.")
    elif user[4] == "disabled":
        await update.message.reply_text("⛔ Akun dinonaktifkan oleh admin.")
    else:
        await show_menu(update.effective_message, bool(user[5]))


async def show_menu(message, is_admin=False):
    rows = [
        [InlineKeyboardButton("➕ Tambah Aktivitas", callback_data="menu:add")],
        [InlineKeyboardButton("📊 Dashboard WPT", callback_data="menu:progress"),
         InlineKeyboardButton("🕘 Riwayat", callback_data="menu:history")],
        [InlineKeyboardButton("🔐 Login e‑Master (OTP)", callback_data="menu:login")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("👥 Kelola Pegawai", callback_data="menu:users")])
    keyboard = InlineKeyboardMarkup(rows)
    await message.reply_text(
        "✨ *AKTIVITAS HARIAN E‑MASTER*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Kelola aktivitas dan pantau WPT\nlangsung dari Telegram.\n\n"
        "Pilih menu di bawah ini:", parse_mode="Markdown", reply_markup=keyboard)


@private
async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if update.callback_query:
        await update.callback_query.answer()
    status = await message.reply_text("⏳ Membuat sesi login e‑Master baru…")
    try:
        client = get_client(update.effective_user.id)
        # Setiap perintah /login wajib membuat sesi baru dan meminta OTP.
        # Sesi milik pegawai lain tidak terpengaruh.
        client.reset_session()
        needs_otp = client.begin_login()
        if needs_otp:
            await status.edit_text("🔐 *VERIFIKASI OTP*\n\nMasukkan 6 digit kode Google Authenticator.\nPesan OTP akan otomatis dihapus.", parse_mode="Markdown")
            return OTP
        await status.edit_text("✅ Login e‑Master berhasil tanpa permintaan OTP dari server.")
    except EMasterError as exc:
        await status.edit_text(f"❌ {exc}")
    return ConversationHandler.END


@private
async def otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    try:
        client = get_client(update.effective_user.id)
        client.submit_otp(code)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Tambah Aktivitas", callback_data="menu:add"),
                                    InlineKeyboardButton("📊 Dashboard", callback_data="menu:progress")]])
        await context.bot.send_message(update.effective_user.id, "✅ *LOGIN BERHASIL*\nSesi e‑Master sudah aktif.", parse_mode="Markdown", reply_markup=kb)
    except EMasterError as exc:
        await context.bot.send_message(update.effective_user.id, f"❌ {exc}")
    return ConversationHandler.END


@private
async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if update.callback_query:
        await update.callback_query.answer()
    client = get_client(update.effective_user.id)
    status = await message.reply_text("⏳ Menyiapkan formulir aktivitas…")
    if not await asyncio.to_thread(client.is_authenticated):
        await status.edit_text("🔐 Sesi belum aktif. Tekan Login terlebih dahulu.",
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Login", callback_data="menu:login")]]))
        return ConversationHandler.END
    try:
        await status.delete()
    except Exception:
        pass
    context.user_data.clear()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Hari Ini", callback_data="date:today"),
         InlineKeyboardButton("↩️ Kemarin", callback_data="date:yesterday")],
        [InlineKeyboardButton("❌ Batal", callback_data="date:cancel")]
    ])
    await message.reply_text("📅 *PILIH TANGGAL AKTIVITAS*\n\nPilih tombol cepat atau ketik DD/MM/YYYY.",
                             parse_mode="Markdown", reply_markup=kb)
    return DATE


@private
async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_date = update.message.text.strip().replace("-", "/")
        date = datetime.strptime(raw_date, "%d/%m/%Y")
    except ValueError:
        await update.message.reply_text("⚠️ Format tidak valid.\nContoh yang benar: `18/07/2026`", parse_mode="Markdown")
        return DATE
    return await load_targets(update, context, date)


@private
async def quick_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    if action == "cancel":
        await query.edit_message_text("❌ Pengisian dibatalkan.")
        return ConversationHandler.END
    date = datetime.now() if action == "today" else datetime.now() - timedelta(days=1)
    await query.edit_message_text(f"📅 Tanggal dipilih: *{date:%d/%m/%Y}*", parse_mode="Markdown")
    return await load_targets(update, context, date)


async def load_targets(update: Update, context: ContextTypes.DEFAULT_TYPE, date: datetime):
    message = update.effective_message
    client = get_client(update.effective_user.id)
    if (datetime.now().date() - date.date()).days > 7:
        await message.reply_text("⚠️ Tanggal melewati batas H+7. Silakan pilih tanggal lain.")
        return DATE
    context.user_data["date"] = date.strftime("%d/%m/%Y")
    try:
        targets = client.list_work_targets(date.strftime("%m"))
    except AuthenticationRequired:
        await message.reply_text("🔐 Sesi habis. Jalankan /login lalu ulangi.")
        return ConversationHandler.END
    except EMasterError as exc:
        await message.reply_text(f"❌ {exc}")
        return ConversationHandler.END
    if not targets:
        await message.reply_text("❌ Kegiatan Tugas Jabatan tidak ditemukan untuk bulan tersebut.")
        return ConversationHandler.END
    context.user_data["targets"] = targets
    buttons = [[InlineKeyboardButton(f"{i+1}. {t.name[:48]}", callback_data=f"target:{i}")]
               for i, t in enumerate(targets)]
    await message.reply_text("🎯 *PILIH TUGAS JABATAN*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    return TARGET


@private
async def pick_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Tugas jabatan dipilih ✅")
    try:
        idx = int(query.data.split(":")[1])
        target: WorkTarget = context.user_data["targets"][idx]
    except (KeyError, IndexError, ValueError):
        await query.message.reply_text("⚠️ Daftar tugas sudah kedaluwarsa. Tekan /tambah untuk memulai kembali.")
        return ConversationHandler.END
    context.user_data["target"] = target
    try:
        await query.edit_message_text(f"✅ TUGAS JABATAN DIPILIH\n\n{target.name}")
    except Exception:
        logging.exception("Tidak dapat mengedit pesan pilihan tugas")
        await query.message.reply_text(f"✅ TUGAS JABATAN DIPILIH\n\n{target.name}")
    await query.message.reply_text("🔎 *CARI AKTIVITAS*\nKetik kata kunci, misalnya: `video`, `rapat`, atau `dokumen`.", parse_mode="Markdown")
    return SEARCH


@private
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = get_client(update.effective_user.id)
    try:
        items = client.search_kamus(update.message.text.strip())
    except AuthenticationRequired:
        await update.message.reply_text("🔐 Sesi habis. Jalankan /login lalu ulangi.")
        return ConversationHandler.END
    except EMasterError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return SEARCH
    if not items:
        await update.message.reply_text("Tidak ditemukan. Coba kata kunci lain.")
        return SEARCH
    context.user_data["items"] = items
    buttons = [[InlineKeyboardButton(f"{x.code} — {x.activity} ({x.wpt} mnt)", callback_data=f"pick:{i}")]
               for i, x in enumerate(items)]
    await update.message.reply_text("📚 *HASIL KAMUS AKTIVITAS*\nPilih salah satu:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    return PICK


@private
async def pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    item: KamusItem = context.user_data["items"][idx]
    context.user_data["item"] = item
    await query.edit_message_text(f"✅ AKTIVITAS DIPILIH\n\n{item.code} — {item.activity}\n📦 Satuan: {item.unit}\n⏱ WPT: {item.wpt} menit")
    await query.message.reply_text("🔢 Masukkan volume (angka bulat):")
    return VOLUME


@private
async def volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = int(update.message.text.strip())
        if value < 1 or value > 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Volume harus angka bulat 1–100.")
        return VOLUME
    item = context.user_data["item"]
    if item.wpt * value > 660:
        await update.message.reply_text(f"❌ Total {item.wpt * value} menit melebihi batas harian 660 menit. Masukkan volume lebih kecil.")
        return VOLUME
    context.user_data["volume"] = value
    await update.message.reply_text(f"📝 Masukkan objek kerja/topik.\nContoh dari kamus: {item.object_hint}")
    return OBJECT


@private
async def object_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) < 5:
        await update.message.reply_text("Objek kerja terlalu pendek.")
        return OBJECT
    context.user_data["object"] = text
    item = context.user_data["item"]
    vol = context.user_data["volume"]
    summary = (f"📋 KONFIRMASI AKTIVITAS\n━━━━━━━━━━━━━━━━━━━━\n"
               f"📅 Tanggal: {context.user_data['date']}\n\n"
               f"🎯 Tugas Jabatan:\n{context.user_data['target'].name}\n\n"
               f"📌 Aktivitas:\n{item.code} — {item.activity}\n\n"
               f"📦 Satuan: {item.unit}\n⏱ WPT: {item.wpt} × {vol} = {item.wpt*vol} menit\n\n"
               f"📝 Objek Kerja:\n{text}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Kirim ke e‑Master", callback_data="send"),
                                InlineKeyboardButton("❌ Batal", callback_data="cancel")]])
    # Teks dari e-Master dapat berisi underscore seperti eastjavatrip_id.
    # Jangan gunakan Markdown agar seluruh karakter selalu aman ditampilkan.
    await update.message.reply_text(summary, reply_markup=kb)
    return CONFIRM


@private
async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Pengisian dibatalkan.")
        return ConversationHandler.END
    item = context.user_data["item"]
    date = context.user_data["date"]
    try:
        client = get_client(update.effective_user.id)
        client.submit_activity(
            month=date[3:5], target=context.user_data["target"], date=date, item=item,
            volume=context.user_data["volume"], object_work=context.user_data["object"])
        storage.add_sent(update.effective_user.id, date, item, context.user_data["volume"], context.user_data["object"])
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Tambah Lagi", callback_data="menu:add"),
                                    InlineKeyboardButton("📊 Dashboard", callback_data="menu:progress")],
                                   [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")]])
        await query.edit_message_text(f"✅ BERHASIL TERSIMPAN\n\n{item.activity}\n⏱ {item.wpt * context.user_data['volume']} menit\n📅 {date}",
                                      reply_markup=kb)
    except AuthenticationRequired:
        await query.edit_message_text("🔐 Sesi habis. Jalankan /login, lalu ulangi pengiriman.")
    except (EMasterError, KeyError) as exc:
        await query.edit_message_text(f"❌ Gagal mengirim: {exc}")
    return ConversationHandler.END


@private
async def progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    message = update.effective_message
    now = datetime.now()
    try:
        client = get_client(update.effective_user.id)
        current = client.get_month_progress(now.strftime("%m"))
        count, minutes = current.activities, current.minutes
        source = "Data terbaru e‑Master"
    except AuthenticationRequired:
        await message.reply_text("🔐 Sesi e‑Master habis. Jalankan /login, lalu buka dashboard kembali.")
        return
    except EMasterError as exc:
        await message.reply_text(f"❌ Tidak dapat memperbarui progres: {exc}")
        return
    target = 6750
    pct = min(100, minutes / target * 100)
    filled = min(10, round(pct / 10))
    bar = "🟩" * filled + "⬜" * (10-filled)
    remaining = max(0, target-minutes)
    days = (remaining + 329) // 330
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Perbarui", callback_data="menu:progress"),
                                InlineKeyboardButton("➕ Tambah", callback_data="menu:add")],
                               [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")]])
    await message.reply_text(
        f"📊 *DASHBOARD WPT — {now:%B %Y}*\n━━━━━━━━━━━━━━━━━━━━\n"
        f"{bar}\n*{pct:.1f}% tercapai*\n\n"
        f"📚 Jumlah aktivitas: *{count}*\n⏱ WPT terkumpul: *{minutes:,} menit*\n"
        f"🎯 Target: *{target:,} menit*\n📉 Kekurangan: *{remaining:,} menit*\n"
        f"📆 Estimasi: *{days} hari × 330 menit*\n\n🔄 {source}",
        parse_mode="Markdown", reply_markup=kb)


@private
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    rows = storage.recent(update.effective_user.id, 8)
    if not rows:
        await update.effective_message.reply_text("🕘 Belum ada aktivitas yang dikirim melalui bot.")
        return
    lines = ["🕘 RIWAYAT TERAKHIR", "━━━━━━━━━━━━━━━━━━━━"]
    for date, activity, wpt, vol, obj in rows:
        lines.append(f"\n📅 {date} · {wpt*vol} menit\n{activity}\n{obj}")
    await update.effective_message.reply_text("\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Tambah Aktivitas", callback_data="menu:add"),
                                            InlineKeyboardButton("🏠 Menu", callback_data="menu:home")]]))


@private
async def menu_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user = storage.get_user(update.effective_user.id)
    await show_menu(update.effective_message, bool(user and user[5]))


def admin_only(update: Update) -> bool:
    user = storage.get_user(update.effective_user.id)
    return bool(user and user[5] and user[4] == "active")


async def add_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not admin_only(update):
        await update.effective_message.reply_text("⛔ Khusus admin.")
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text(
        "👥 *TAMBAH PEGAWAI*\n\nMasukkan Telegram ID pegawai.\nPegawai dapat melihat ID melalui `/start`.",
        parse_mode="Markdown")
    return ADMIN_TGID


async def employee_tgid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        telegram_id = int(update.message.text.strip())
        if telegram_id <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Telegram ID harus berupa angka.")
        return ADMIN_TGID
    context.user_data["new_telegram_id"] = telegram_id
    await update.message.reply_text("Masukkan NIP pegawai:")
    return ADMIN_NIP


async def employee_nip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nip = update.message.text.strip()
    if not nip.isdigit() or len(nip) < 10:
        await update.message.reply_text("NIP tidak valid. Masukkan angka NIP lengkap.")
        return ADMIN_NIP
    context.user_data["new_nip"] = nip
    try:
        await update.message.delete()
    except Exception:
        pass
    await context.bot.send_message(update.effective_user.id, "Masukkan nama pegawai:")
    return ADMIN_NAME


async def employee_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    telegram_id = context.user_data["new_telegram_id"]
    storage.invite_user(telegram_id, context.user_data["new_nip"], name)
    await update.message.reply_text(
        f"✅ UNDANGAN DIBUAT\n\nNama: {name}\nTelegram ID: {telegram_id}\n\n"
        "Minta pegawai membuka bot, tekan /start, lalu /aktifkan.")
    try:
        await context.bot.send_message(telegram_id,
            f"👋 Halo {name}, Anda telah didaftarkan ke Bot Aktivitas e‑Master.\nJalankan /aktifkan untuk memasukkan password pribadi.")
    except Exception:
        pass
    context.user_data.clear()
    return ConversationHandler.END


async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = storage.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("⛔ Telegram ID Anda belum didaftarkan admin.")
        return ConversationHandler.END
    if user[4] == "active":
        await update.message.reply_text("✅ Akun sudah aktif. Gunakan /login.")
        return ConversationHandler.END
    if user[4] != "invited":
        await update.message.reply_text("⛔ Akun tidak dapat diaktifkan. Hubungi admin.")
        return ConversationHandler.END
    await update.message.reply_text(
        "🔐 *AKTIVASI AKUN*\n\nKirim password e‑Master Anda. Pesan akan langsung dihapus dan password disimpan terenkripsi.",
        parse_mode="Markdown")
    return ACTIVATE_PASSWORD


async def activate_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text
    try:
        await update.message.delete()
    except Exception:
        pass
    if len(password) < 4:
        await context.bot.send_message(update.effective_user.id, "Password terlalu pendek. Jalankan /aktifkan kembali.")
        return ConversationHandler.END
    encrypted = fernet.encrypt(password.encode()).decode()
    storage.activate_user(update.effective_user.id, encrypted)
    clients.pop(update.effective_user.id, None)
    await context.bot.send_message(update.effective_user.id,
        "✅ *AKUN AKTIF*\nPassword telah dienkripsi. Sekarang jalankan /login dan masukkan OTP Anda.", parse_mode="Markdown")
    return ConversationHandler.END


async def users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not admin_only(update):
        await update.effective_message.reply_text("⛔ Khusus admin.")
        return
    rows = storage.list_users()
    text = ["👥 KELOLA PEGAWAI", "━━━━━━━━━━━━━━━━━━━━"]
    buttons = [[InlineKeyboardButton("➕ Tambah Pegawai", callback_data="admin:add")]]
    for tid, nip, name, status, is_admin in rows:
        icon = "👑" if is_admin else ("✅" if status == "active" else "⏳" if status == "invited" else "⛔")
        text.append(f"\n{icon} {name or 'Tanpa nama'}\nID: {tid} · Status: {status}")
        if not is_admin and status != "disabled":
            buttons.append([InlineKeyboardButton(f"⛔ Nonaktifkan {name or tid}", callback_data=f"admin:disable:{tid}")])
    await update.effective_message.reply_text("\n".join(text), reply_markup=InlineKeyboardMarkup(buttons))


async def disable_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if not admin_only(update):
        return
    telegram_id = int(update.callback_query.data.split(":")[2])
    storage.disable_user(telegram_id)
    clients.pop(telegram_id, None)
    await update.callback_query.edit_message_text("✅ Pegawai dinonaktifkan. Data pegawai lain tidak berubah.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.exception("Unhandled Telegram bot error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Terjadi gangguan saat memproses tombol. Silakan tekan /tambah dan coba kembali.")
        except Exception:
            pass


@private
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Pengisian dibatalkan.")
    return ConversationHandler.END


def main():
    app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("tambahpegawai", add_employee),
                      CallbackQueryHandler(add_employee, pattern=r"^admin:add$")],
        states={ADMIN_TGID: [MessageHandler(filters.TEXT & ~filters.COMMAND, employee_tgid)],
                ADMIN_NIP: [MessageHandler(filters.TEXT & ~filters.COMMAND, employee_nip)],
                ADMIN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, employee_name)]},
        fallbacks=[CommandHandler("batal", cancel)]))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("aktifkan", activate)],
        states={ACTIVATE_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, activate_password)]},
        fallbacks=[CommandHandler("batal", cancel)]))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("login", login),
                      CallbackQueryHandler(login, pattern=r"^menu:login$"),
                      CommandHandler("tambah", add),
                      CallbackQueryHandler(add, pattern=r"^menu:add$")],
        states={
        OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp)],
        DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date),
               CallbackQueryHandler(quick_date, pattern=r"^date:")],
        TARGET: [CallbackQueryHandler(pick_target, pattern=r"^target:\d+$")],
        SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, search)],
        PICK: [CallbackQueryHandler(pick, pattern=r"^pick:\d+$")],
        VOLUME: [MessageHandler(filters.TEXT & ~filters.COMMAND, volume)],
        OBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, object_work)],
        CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^(send|cancel)$")],
    }, fallbacks=[CommandHandler("batal", cancel)], allow_reentry=True))
    app.add_handler(CommandHandler("progres", progress))
    app.add_handler(CommandHandler("dashboard", progress))
    app.add_handler(CommandHandler("riwayat", history))
    app.add_handler(CommandHandler("batal", cancel))
    app.add_handler(CallbackQueryHandler(progress, pattern=r"^menu:progress$"))
    app.add_handler(CallbackQueryHandler(history, pattern=r"^menu:history$"))
    app.add_handler(CallbackQueryHandler(menu_home, pattern=r"^menu:home$"))
    app.add_handler(CallbackQueryHandler(users_menu, pattern=r"^menu:users$"))
    app.add_handler(CallbackQueryHandler(disable_employee, pattern=r"^admin:disable:\d+$"))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
