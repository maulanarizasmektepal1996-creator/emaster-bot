from __future__ import annotations

import logging
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

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
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Aktivitas", callback_data="menu:add"),
         InlineKeyboardButton("📊 Dashboard WPT", callback_data="menu:progress")],
        [InlineKeyboardButton("🕘 Riwayat e‑Master", callback_data="menu:history"),
         InlineKeyboardButton("⭐ Favorit", callback_data="menu:favorites")],
        [InlineKeyboardButton("🔐 Login / Cek Sesi", callback_data="menu:login")]
    ])
    await update.message.reply_text("🤖 Bot Aktivitas Harian e‑Master\nPilih menu:", reply_markup=kb)


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
    if context.user_data.get("favorite_mode") and context.user_data.get("item"):
        item = context.user_data["item"]
        await query.message.reply_text(
            f"⭐ Favorit: {item.activity}\nWPT: {item.wpt} menit\nMasukkan volume (angka bulat):")
        return VOLUME
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
        before = client.get_month_progress(date[3:5])
        client.submit_activity(
            month=date[3:5], target=context.user_data["target"], date=date, item=item,
            volume=context.user_data["volume"], object_work=context.user_data["object"])
        storage.add_sent(date, item, context.user_data["volume"], context.user_data["object"])
        after = client.get_month_progress(date[3:5])
        expected = item.wpt * context.user_data["volume"]
        verified = after.minutes - before.minutes == expected
        mark = "✅ TERVERIFIKASI" if verified else "⚠️ PERLU DIPERIKSA"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Simpan sebagai Favorit", callback_data=f"favlast:{storage.recent(1)[0][0]}")],
                                   [InlineKeyboardButton("📊 Lihat Dashboard", callback_data="menu:progress")]])
        await query.edit_message_text(
            f"{mark}\nAktivitas tersimpan di e‑Master.\nWPT sebelumnya: {before.minutes}\n"
            f"WPT sekarang: {after.minutes}\nPenambahan: {after.minutes-before.minutes} menit",
            reply_markup=kb)
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
        await update.effective_message.reply_text("🔐 Sesi e‑Master habis. Jalankan /login, lalu /progres kembali.")
        return
    except EMasterError as exc:
        await update.effective_message.reply_text(f"❌ Tidak dapat memperbarui progres: {exc}")
        return
    target = 6750
    pct = min(100, minutes / target * 100)
    bar = "█" * round(pct / 10) + "░" * (10 - round(pct / 10))
    days_needed = (max(0, target-minutes) + 329) // 330
    await update.effective_message.reply_text(
        f"📊 DASHBOARD WPT — {now:%B %Y}\n\n{bar} {pct:.1f}%\n"
        f"Aktivitas: {count}\nWPT: {minutes}/{target} menit\n"
        f"Kekurangan: {max(0,target-minutes)} menit\nEstimasi: {days_needed} hari × 330 menit\n\n🔄 {source}")


@private
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = client.list_recent_activities(datetime.now().strftime("%m"), 8)
    except AuthenticationRequired:
        await update.effective_message.reply_text("🔐 Sesi habis. Jalankan /login.")
        return
    except EMasterError as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return
    if not rows:
        await update.effective_message.reply_text("Belum ada aktivitas yang ditemukan.")
        return
    context.user_data["remote_history"] = {x.realization_id: x for x in rows}
    for x in rows:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Hapus", callback_data=f"askdel:{x.realization_id}")]])
        await update.effective_message.reply_text(
            f"📅 {x.date}\n{x.activity}\n{x.object_work}\n{x.wpt} × {x.volume} = {x.total} menit",
            reply_markup=kb)


@private
async def ask_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rid = q.data.split(":")[1]
    row = context.user_data.get("remote_history", {}).get(rid)
    if not row:
        await q.edit_message_text("Riwayat kedaluwarsa. Buka /riwayat kembali.")
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Ya, hapus permanen", callback_data=f"dodel:{rid}"),
                                InlineKeyboardButton("Batal", callback_data="dodel:cancel")]])
    await q.edit_message_reply_markup(kb)


@private
async def do_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rid = q.data.split(":")[1]
    if rid == "cancel":
        await q.edit_message_text("Penghapusan dibatalkan.")
        return
    row = context.user_data.get("remote_history", {}).get(rid)
    if not row:
        await q.edit_message_text("Riwayat kedaluwarsa. Tidak ada data yang dihapus.")
        return
    try:
        client.delete_activity(row.delete_url)
        await q.edit_message_text("✅ Aktivitas dihapus dari e‑Master. Gunakan /progres untuk memperbarui WPT.")
    except EMasterError as exc:
        await q.edit_message_text(f"❌ Gagal menghapus: {exc}")


@private
async def favorite_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    local_id = int(q.data.split(":")[1])
    row = next((x for x in storage.recent(30) if x[0] == local_id), None)
    if not row:
        await q.answer("Data lokal tidak ditemukan", show_alert=True); return
    storage.add_favorite(row[2], row[3], row[4], row[5], row[7])
    await q.edit_message_reply_markup(InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Tersimpan di Favorit", callback_data="menu:favorites")]]))


@private
async def favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = storage.favorites()
    if not rows:
        await update.effective_message.reply_text("⭐ Belum ada favorit. Setelah mengirim aktivitas, tekan ‘Simpan sebagai Favorit’.")
        return
    for row in rows:
        await update.effective_message.reply_text(
            f"⭐ {row[2]}\nWPT: {row[4]} menit\nObjek: {row[5]}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Gunakan", callback_data=f"usefav:{row[0]}")],
                [InlineKeyboardButton("🗑 Hapus Favorit", callback_data=f"delfav:{row[0]}")]
            ]))


@private
async def use_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    favorite_id = int(q.data.split(":")[1])
    row = next((x for x in storage.favorites() if x[0] == favorite_id), None)
    if not row:
        await q.edit_message_text("Favorit tidak ditemukan.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["favorite_mode"] = True
    context.user_data["item"] = KamusItem(row[1], row[2], row[3], row[4], "Favorit", row[5])
    await q.message.reply_text("📅 Masukkan tanggal aktivitas (DD/MM/YYYY):")
    return DATE


@private
async def delete_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    storage.delete_favorite(int(q.data.split(":")[1]))
    await q.edit_message_text("Favorit dihapus.")


async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    try:
        p = client.get_month_progress(datetime.now().strftime("%m"))
        remaining = max(0, 6750-p.minutes)
        text = (f"⏰ Pengingat Aktivitas Harian\nWPT e‑Master: {p.minutes}/6750 menit\n"
                f"Kekurangan: {remaining} menit\n" + ("Target bulanan sudah terpenuhi ✅" if not remaining else "Jangan lupa isi aktivitas hari ini."))
    except Exception:
        text = "⏰ Jangan lupa memeriksa dan mengisi aktivitas harian e‑Master."
    await context.bot.send_message(OWNER, text)


@private
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    action = q.data.split(":")[1]
    if action == "progress": return await progress(update, context)
    if action == "history": return await history(update, context)
    if action == "favorites": return await favorites(update, context)
    if action == "add":
        await q.message.reply_text("Gunakan /tambah untuk mulai mengisi aktivitas.")
    elif action == "login":
        await q.message.reply_text("Gunakan /login untuk mengecek atau memperbarui sesi.")


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
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("tambah", add),
                                                    CallbackQueryHandler(use_favorite, pattern=r"^usefav:\d+$")], states={
        DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
        TARGET: [CallbackQueryHandler(pick_target, pattern=r"^target:\d+$")],
        SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, search)],
        PICK: [CallbackQueryHandler(pick, pattern=r"^pick:\d+$")],
        VOLUME: [MessageHandler(filters.TEXT & ~filters.COMMAND, volume)],
        OBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, object_work)],
        CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^(send|cancel)$")],
    }, fallbacks=[CommandHandler("batal", cancel)]))
    app.add_handler(CommandHandler("progres", progress))
    app.add_handler(CommandHandler("dashboard", progress))
    app.add_handler(CommandHandler("riwayat", history))
    app.add_handler(CommandHandler("favorit", favorites))
    app.add_handler(CommandHandler("batal", cancel))
    app.add_handler(CallbackQueryHandler(menu_router, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(favorite_last, pattern=r"^favlast:\d+$"))
    app.add_handler(CallbackQueryHandler(ask_delete, pattern=r"^askdel:\d+$"))
    app.add_handler(CallbackQueryHandler(do_delete, pattern=r"^dodel:"))
    app.add_handler(CallbackQueryHandler(delete_favorite, pattern=r"^delfav:\d+$"))
    if app.job_queue:
        tz = ZoneInfo(os.getenv("TZ", "Asia/Jakarta"))
        app.job_queue.run_daily(daily_reminder, time(hour=int(os.getenv("REMINDER_HOUR", "15")),
                                                      minute=int(os.getenv("REMINDER_MINUTE", "30")), tzinfo=tz))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
