from __future__ import annotations

import logging
import os
from datetime import datetime

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
client = EMasterClient(os.environ["EMASTER_NIP"], os.environ["EMASTER_PASSWORD"],
                       os.environ["ENCRYPTION_KEY"], os.getenv("SESSION_PATH", "/data/emaster_session.bin"))
storage = Storage(os.getenv("DATABASE_PATH", "/data/emaster_bot.db"))

DATE, TARGET, SEARCH, PICK, VOLUME, OBJECT, CONFIRM, OTP = range(8)


def private(fn):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or update.effective_user.id != OWNER:
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Bot pribadi. Akses ditolak.")
            return ConversationHandler.END
        return await fn(update, context)
    return wrapped


@private
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot Aktivitas e‑Master\n\n"
        "/login — hubungkan sesi e‑Master\n"
        "/tambah — tambah dan kirim aktivitas\n"
        "/progres — progres bulan berjalan\n"
        "/batal — batalkan pengisian")


@private
async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if client.is_authenticated():
            await update.message.reply_text("✅ Sesi e‑Master masih aktif.")
            return ConversationHandler.END
        needs_otp = client.begin_login()
        if needs_otp:
            await update.message.reply_text("🔐 Masukkan OTP Google Authenticator 6 digit. Pesan akan dihapus setelah diproses.")
            return OTP
        await update.message.reply_text("✅ Login e‑Master berhasil.")
    except EMasterError as exc:
        await update.message.reply_text(f"❌ {exc}")
    return ConversationHandler.END


@private
async def otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    try:
        client.submit_otp(code)
        await context.bot.send_message(OWNER, "✅ OTP diterima. Sesi e‑Master aktif.")
    except EMasterError as exc:
        await context.bot.send_message(OWNER, f"❌ {exc}")
    return ConversationHandler.END


@private
async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not client.is_authenticated():
        await update.message.reply_text("🔐 Sesi belum aktif. Jalankan /login terlebih dahulu.")
        return ConversationHandler.END
    await update.message.reply_text("📅 Masukkan tanggal aktivitas (DD/MM/YYYY), contoh: 18/07/2026")
    return DATE


@private
async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_date = update.message.text.strip().replace("-", "/")
        date = datetime.strptime(raw_date, "%d/%m/%Y")
    except ValueError:
        await update.message.reply_text("Format tidak valid. Gunakan DD/MM/YYYY.")
        return DATE
    if (datetime.now().date() - date.date()).days > 7:
        await update.message.reply_text("⚠️ Tanggal lebih dari H+7. e‑Master kemungkinan menolak. Ketik tanggal lain.")
        return DATE
    context.user_data["date"] = date.strftime("%d/%m/%Y")
    try:
        targets = client.list_work_targets(date.strftime("%m"))
    except AuthenticationRequired:
        await update.message.reply_text("🔐 Sesi habis. Jalankan /login lalu ulangi.")
        return ConversationHandler.END
    except EMasterError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return ConversationHandler.END
    if not targets:
        await update.message.reply_text("❌ Kegiatan Tugas Jabatan tidak ditemukan untuk bulan tersebut.")
        return ConversationHandler.END
    context.user_data["targets"] = targets
    buttons = [[InlineKeyboardButton(t.name[:55], callback_data=f"target:{i}")]
               for i, t in enumerate(targets)]
    await update.message.reply_text("Pilih Kegiatan Tugas Jabatan:", reply_markup=InlineKeyboardMarkup(buttons))
    return TARGET


@private
async def pick_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    target: WorkTarget = context.user_data["targets"][idx]
    context.user_data["target"] = target
    await query.edit_message_text(f"✅ Tugas jabatan:\n{target.name}")
    await query.message.reply_text("🔎 Ketik kata kunci kamus aktivitas, misalnya: video, rapat, dokumen")
    return SEARCH


@private
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text("Pilih aktivitas:", reply_markup=InlineKeyboardMarkup(buttons))
    return PICK


@private
async def pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    item: KamusItem = context.user_data["items"][idx]
    context.user_data["item"] = item
    await query.edit_message_text(f"✅ {item.code} — {item.activity}\nSatuan: {item.unit}\nWPT: {item.wpt} menit")
    await query.message.reply_text("Masukkan volume (angka bulat):")
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
    summary = (f"📋 KONFIRMASI\n\nTanggal: {context.user_data['date']}\n"
               f"Tugas jabatan: {context.user_data['target'].name}\n"
               f"Aktivitas: {item.code} — {item.activity}\nSatuan: {item.unit}\n"
               f"WPT: {item.wpt} × {vol} = {item.wpt*vol} menit\nObjek kerja: {text}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Kirim ke e‑Master", callback_data="send"),
                                InlineKeyboardButton("❌ Batal", callback_data="cancel")]])
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
        client.submit_activity(
            month=date[3:5], target=context.user_data["target"], date=date, item=item,
            volume=context.user_data["volume"], object_work=context.user_data["object"])
        storage.add_sent(date, item, context.user_data["volume"], context.user_data["object"])
        await query.edit_message_text("✅ Aktivitas berhasil dikirim ke e‑Master.")
    except AuthenticationRequired:
        await query.edit_message_text("🔐 Sesi habis. Jalankan /login, lalu ulangi pengiriman.")
    except (EMasterError, KeyError) as exc:
        await query.edit_message_text(f"❌ Gagal mengirim: {exc}")
    return ConversationHandler.END


@private
async def progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    try:
        current = client.get_month_progress(now.strftime("%m"))
        count, minutes = current.activities, current.minutes
        source = "Data terbaru e‑Master"
    except AuthenticationRequired:
        await update.message.reply_text("🔐 Sesi e‑Master habis. Jalankan /login, lalu /progres kembali.")
        return
    except EMasterError as exc:
        await update.message.reply_text(f"❌ Tidak dapat memperbarui progres: {exc}")
        return
    target = 6750
    pct = min(100, minutes / target * 100)
    await update.message.reply_text(f"📊 Progres {now:%B %Y}\n{source}\nJumlah aktivitas: {count}\nWPT: {minutes}/{target} menit ({pct:.1f}%)\nKekurangan: {max(0,target-minutes)} menit")


@private
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Pengisian dibatalkan.")
    return ConversationHandler.END


def main():
    app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("login", login)],
        states={OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp)]}, fallbacks=[CommandHandler("batal", cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("tambah", add)], states={
        DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
        TARGET: [CallbackQueryHandler(pick_target, pattern=r"^target:\d+$")],
        SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, search)],
        PICK: [CallbackQueryHandler(pick, pattern=r"^pick:\d+$")],
        VOLUME: [MessageHandler(filters.TEXT & ~filters.COMMAND, volume)],
        OBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, object_work)],
        CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^(send|cancel)$")],
    }, fallbacks=[CommandHandler("batal", cancel)]))
    app.add_handler(CommandHandler("progres", progress))
    app.add_handler(CommandHandler("batal", cancel))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
