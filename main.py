from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sqlite3
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputFile, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler, filters)

from emaster import (AuthenticationRequired, EMasterActivity, EMasterClient,
                     EMasterError, KamusItem, WorkTarget)
from drive_storage import DriveStorageError, GoogleDriveStorage
from staff_directory import LocalStaffDirectory, StaffDirectoryError
from storage import Storage
from wfh_report import (EvidenceFile, ReportActivity, build_wfh_report,
                        convert_docx_to_pdf,
                        format_indonesian_date, now_jakarta, report_date_for_access,
                        report_file_name, report_generation_allowed,
                        report_pdf_file_name, tidy_sentence)

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

OWNER = int(os.environ["TELEGRAM_USER_ID"])
storage = Storage(os.getenv("DATABASE_PATH", "/data/emaster_bot.db"))
fernet = Fernet(os.environ["ENCRYPTION_KEY"].encode())
storage.ensure_admin(OWNER, os.environ["EMASTER_NIP"],
                     fernet.encrypt(os.environ["EMASTER_PASSWORD"].encode()).decode())
storage.claim_legacy_activities(OWNER)
storage.claim_legacy_favorites(OWNER)
staff_directory = LocalStaffDirectory()
clients: dict[int, EMasterClient] = {}
report_locks: dict[int, asyncio.Lock] = {}
session_dir = Path(os.getenv("SESSION_PATH", "/data/emaster_session.bin")).parent / "sessions"
drive_storage = GoogleDriveStorage()
REPORT_TEMPLATE = Path(__file__).with_name("templates") / "Laporan_WFH_template.docx"
JAKARTA = ZoneInfo("Asia/Jakarta")


@dataclass(frozen=True)
class GeneratedWfhReport:
    word_name: str
    word_url: str
    word_content: bytes
    pdf_name: str
    pdf_url: str
    pdf_content: bytes
    activity_count: int
    signature_included: bool


def clear_cached_user(telegram_id: int) -> None:
    clients.pop(telegram_id, None)
    path = Path(os.getenv("SESSION_PATH", "/data/emaster_session.bin")) if telegram_id == OWNER else session_dir / f"{telegram_id}.bin"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def get_client(telegram_id: int) -> EMasterClient:
    user = storage.get_user(telegram_id)
    if not user or user[4] != "active" or not user[2]:
        raise EMasterError("Akun belum aktif.")
    if telegram_id not in clients:
        try:
            password = fernet.decrypt(user[2].encode()).decode()
        except InvalidToken as exc:
            raise EMasterError(
                "Data login tidak dapat dibuka. ENCRYPTION_KEY kemungkinan berubah. "
                "Admin perlu mendaftarkan ulang akun ini.") from exc
        session_path = os.getenv("SESSION_PATH", "/data/emaster_session.bin") if telegram_id == OWNER else str(session_dir / f"{telegram_id}.bin")
        clients[telegram_id] = EMasterClient(user[1], password, os.environ["ENCRYPTION_KEY"],
                                             session_path)
    return clients[telegram_id]

DATE, TARGET, SEARCH, PICK, VOLUME, OBJECT, CONFIRM, OTP, ADMIN_TGID, ADMIN_NIP, ADMIN_NAME, ACTIVATE_PASSWORD = range(12)
EDIT_ACTIVITY, EDIT_PICK, EDIT_DATE, EDIT_VOLUME, EDIT_OBJECT, EDIT_CONFIRM = range(12, 18)
COPY_DATE, COPY_TARGET, COPY_VOLUME, COPY_OBJECT = range(18, 22)
ADMIN_TYPE, DAILY_ACTIVITY, DAILY_CONFIRM, ADDITIONAL_PHOTO = range(22, 26)
SIGNATURE_UPLOAD = 26
KAMUS_PAGE_SIZE = 8
HISTORY_PAGE_SIZE = 5


def employee_type(user) -> str:
    return user[8] if user and len(user) > 8 and user[8] in {"asn", "non_asn"} else "asn"


def employee_unit(user) -> str:
    unit = user[9] if user and len(user) > 9 and user[9] else ""
    if not unit or unit.strip().lower() == "pemasaran":
        return "Bidang Pemasaran dan Kelembagaan Parekraf"
    return unit


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def current_activity_period() -> tuple[datetime, datetime]:
    first = now_jakarta().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first.month == 12:
        next_month = first.replace(year=first.year + 1, month=1)
    else:
        next_month = first.replace(month=first.month + 1)
    return first, next_month - timedelta(days=1)


def parse_activity_date(value: str) -> datetime:
    """Terima seluruh tanggal pada bulan berjalan, termasuk tanggal mendatang."""
    date = datetime.strptime(value.strip().replace("-", "/"), "%d/%m/%Y")
    first, last = current_activity_period()
    if not first.date() <= date.date() <= last.date():
        raise ValueError(
            f"Tanggal harus berada dalam periode {first:%d/%m/%Y}–{last:%d/%m/%Y}.")
    return date


def activity_date_iso(value: str) -> str:
    return datetime.strptime(value.strip().replace("/", "-"), "%d-%m-%Y").date().isoformat()


def persist_draft(telegram_id: int, context: ContextTypes.DEFAULT_TYPE, stage: str) -> None:
    payload = {"stage": stage}
    for key in ("date", "volume", "object", "source_chat_id", "source_message_id", "photos"):
        if key in context.user_data:
            payload[key] = context.user_data[key]
    if isinstance(context.user_data.get("target"), WorkTarget):
        payload["target"] = asdict(context.user_data["target"])
    if isinstance(context.user_data.get("item"), KamusItem):
        payload["item"] = asdict(context.user_data["item"])
    storage.save_draft(telegram_id, json.dumps(payload, ensure_ascii=False))


def restore_draft(telegram_id: int, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    row = storage.get_draft(telegram_id)
    if not row:
        return None
    try:
        payload = json.loads(row[0])
        if not isinstance(payload, dict):
            raise ValueError
        context.user_data.clear()
        for key in ("date", "volume", "object", "source_chat_id", "source_message_id", "photos"):
            if key in payload:
                context.user_data[key] = payload[key]
        if isinstance(payload.get("target"), dict):
            context.user_data["target"] = WorkTarget(**payload["target"])
        if isinstance(payload.get("item"), dict):
            context.user_data["item"] = KamusItem(**payload["item"])
        return payload
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        storage.delete_draft(telegram_id)
        return None


async def day_assessment(client: EMasterClient, date: str, activity_name: str,
                         object_work: str, exclude_id: str | None = None):
    rows = await asyncio.to_thread(client.list_activities, date[3:5], 200)
    normalized_date = date.replace("/", "-")
    same_day = [row for row in rows
                if row.date.replace("/", "-") == normalized_date and row.id_realisasi != exclude_id]
    minutes = sum(row.total_minutes for row in same_day)
    duplicates = [row for row in same_day
                  if normalize_text(row.detail) == normalize_text(activity_name)
                  and normalize_text(row.object_work) == normalize_text(object_work)]
    return minutes, duplicates


def private(fn):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = storage.get_user(update.effective_user.id) if update.effective_user else None
        if not user or user[4] != "active":
            if update.callback_query:
                await update.callback_query.answer("Akun belum aktif.", show_alert=True)
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Akun belum aktif. Minta admin mendaftarkan Telegram ID Anda, lalu gunakan /aktifkan.")
            return ConversationHandler.END
        if storage.maintenance_active() and not user[5]:
            if update.callback_query:
                await update.callback_query.answer("Bot sedang dalam perbaikan.", show_alert=True)
            if update.effective_message:
                await update.effective_message.reply_text(
                    "🔧 BOT SEDANG DALAM PERBAIKAN\n\n"
                    "E-Master Jatim sedang menjalani pembaruan sistem. "
                    "Silakan gunakan kembali setelah notifikasi selesai perbaikan dikirim.")
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
    elif storage.maintenance_active() and not user[5]:
        await update.message.reply_text(
            "🔧 BOT SEDANG DALAM PERBAIKAN\n\n"
            "E-Master Jatim sedang menjalani pembaruan sistem. "
            "Mohon menunggu sampai perbaikan selesai.")
    else:
        user = await sync_employee_profile(update.effective_user.id)
        await show_menu(update.effective_message, user)


def menu_content(user, reveal_nip: bool = False):
    kind = employee_type(user)
    rows = [
        [InlineKeyboardButton("➕ Tambah Aktivitas", callback_data="menu:add")],
        [InlineKeyboardButton("📋 Aktivitas Hari Ini", callback_data="daily:today"),
         InlineKeyboardButton("🗓 Riwayat Harian", callback_data="daily:history")],
        [InlineKeyboardButton("📄 Laporan WFH", callback_data="report:generate"),
         InlineKeyboardButton("☁️ Bukti Dukung", callback_data="daily:evidence")],
    ]
    if kind == "asn":
        rows.extend([
            [InlineKeyboardButton("📊 Dashboard WPT", callback_data="menu:progress"),
             InlineKeyboardButton("🕘 Riwayat E-Master", callback_data="menu:history")],
            [InlineKeyboardButton("⭐ Favorit Saya", callback_data="menu:favorites"),
             InlineKeyboardButton("🔐 Login OTP", callback_data="menu:login")],
        ])
    rows.append([InlineKeyboardButton("👤 Profil", callback_data="menu:profileview"),
                 InlineKeyboardButton("❓ Bantuan", callback_data="menu:help")])
    rows.append([InlineKeyboardButton("✍️ Tanda Tangan", callback_data="signature:menu")])
    if kind == "asn":
        rows.append([InlineKeyboardButton("🔄 Perbarui Profil", callback_data="menu:profile")])
    if kind == "asn" and storage.get_draft(user[0]):
        rows.append([InlineKeyboardButton("📝 Lanjutkan Draft", callback_data="menu:resume")])
    if user[5]:
        rows.append([InlineKeyboardButton("👥 Kelola Pegawai", callback_data="menu:users")])
    name = user[3] or "Pegawai"
    nip = user[1]
    shown_nip = nip if reveal_nip else (("•" * max(0, len(nip) - 4)) + nip[-4:])
    position = user[6] or ("Pegawai Non-ASN" if kind == "non_asn" else
                           "Belum terdaftar pada data pegawai — tekan Perbarui Profil")
    identifier_label = "NIP" if kind == "asn" else "ID Pegawai"
    type_label = "ASN" if kind == "asn" else "Non-ASN"
    login_note = "🔐 OTP diperlukan pada setiap login baru.\n" if kind == "asn" else ""
    text = ("🌿 E-MASTER JATIM\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Nama: {name}\n🪪 {identifier_label}: {shown_nip}\n"
            f"🏷 Jenis: {type_label}\n💼 Jabatan: {position}\n"
            f"{login_note}\n"
            "Pilih menu:")
    return text, InlineKeyboardMarkup(rows)


async def show_menu(message, user, edit=False):
    reveal_nip = getattr(getattr(message, "chat", None), "type", "") == "private"
    text, keyboard = menu_content(user, reveal_nip=reveal_nip)
    if edit:
        try:
            await message.edit_text(text, reply_markup=keyboard)
            return
        except Exception:
            logging.debug("Menu lama tidak dapat diedit; mengirim menu baru")
    await message.reply_text(text, reply_markup=keyboard)


async def sync_employee_profile(telegram_id: int, *, strict: bool = False):
    try:
        user = storage.get_user(telegram_id)
        if employee_type(user) == "non_asn":
            return user
        profile = await asyncio.to_thread(staff_directory.find_by_nip, user[1])
        if not profile:
            storage.clear_profile_position(telegram_id)
            raise StaffDirectoryError(
                "NIP/NPPK tidak ditemukan pada DATA PEGAWAI tab update.")
        storage.update_profile(telegram_id, profile.name, profile.position)
    except StaffDirectoryError:
        if strict:
            raise
        logging.warning("Profil lokal belum dapat disinkronkan untuk Telegram ID %s", telegram_id)
    return storage.get_user(telegram_id)


def login_success_content(user):
    menu_text, keyboard = menu_content(user, reveal_nip=True)
    return "✅ LOGIN BERHASIL\nSesi e‑Master sudah aktif.\n\n" + menu_text, keyboard


@private
async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if update.callback_query:
        await update.callback_query.answer()
    user = storage.get_user(update.effective_user.id)
    if employee_type(user) == "non_asn":
        await message.reply_text(
            "ℹ️ Pegawai Non-ASN tidak memerlukan login atau OTP E-Master.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🏠 Menu Utama", callback_data="menu:home")]]))
        return ConversationHandler.END
    status = await message.reply_text("⏳ Membuat sesi login e‑Master baru…")
    try:
        client = get_client(update.effective_user.id)
        # Setiap perintah /login wajib membuat sesi baru dan meminta OTP.
        # Sesi milik pegawai lain tidak terpengaruh.
        client.reset_session()
        needs_otp = await asyncio.to_thread(client.begin_login)
        if needs_otp:
            await status.edit_text("🔐 *VERIFIKASI OTP*\n\nMasukkan 6 digit kode Google Authenticator.\nPesan OTP akan otomatis dihapus.", parse_mode="Markdown")
            return OTP
        user = await sync_employee_profile(update.effective_user.id)
        text, keyboard = login_success_content(user)
        await status.edit_text(text, reply_markup=keyboard)
    except EMasterError as exc:
        await status.edit_text(f"❌ {exc}")
    return ConversationHandler.END


@private
async def otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        logging.warning("Pesan OTP tidak dapat dihapus otomatis; tidak ada isi OTP yang dicatat")
    try:
        client = get_client(update.effective_user.id)
        await asyncio.to_thread(client.submit_otp, code)
        user = await sync_employee_profile(update.effective_user.id)
        text, keyboard = login_success_content(user)
        await context.bot.send_message(update.effective_user.id, text, reply_markup=keyboard)
    except EMasterError as exc:
        await context.bot.send_message(update.effective_user.id, f"❌ {exc}")
    return ConversationHandler.END


@private
async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if update.callback_query:
        await update.callback_query.answer()
    user = storage.get_user(update.effective_user.id)
    if employee_type(user) == "non_asn":
        context.user_data.clear()
        await message.reply_text(
            "📝 TAMBAH AKTIVITAS HARIAN\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Tanggal: {format_indonesian_date(now_jakarta().date())}\n\n"
            "Kirim salah satu:\n"
            "• Uraian aktivitas dalam bentuk pesan teks; atau\n"
            "• Satu foto dengan uraian aktivitas pada caption. Foto tambahan dapat dipilih saat konfirmasi.\n\n"
            "Tuliskan siapa yang terlibat, kegiatan yang dilakukan, serta hasil atau dampaknya.\n"
            "Gunakan /batal untuk membatalkan.")
        return DAILY_ACTIVITY
    forced_new = bool(update.callback_query and update.callback_query.data == "menu:newadd")
    if storage.get_draft(update.effective_user.id) and not forced_new:
        await message.reply_text(
            "📝 DRAFT DITEMUKAN\n━━━━━━━━━━━━━━━━━━━━\n"
            "Lanjutkan pengisian sebelumnya atau mulai aktivitas baru?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Lanjutkan Draft", callback_data="menu:resume")],
                [InlineKeyboardButton("🆕 Mulai Baru", callback_data="menu:newadd"),
                 InlineKeyboardButton("🏠 Menu", callback_data="menu:home")],
            ]))
        return ConversationHandler.END
    client = get_client(update.effective_user.id)
    status = await message.reply_text("⏳ Menyiapkan formulir aktivitas…")
    if not await asyncio.to_thread(client.is_authenticated):
        await status.edit_text("🔐 Sesi belum aktif. Tekan Login terlebih dahulu.",
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Login", callback_data="menu:login")]]))
        return ConversationHandler.END
    try:
        await status.delete()
    except Exception:
        logging.debug("Pesan status formulir tidak dapat dihapus")
    context.user_data.clear()
    storage.delete_draft(update.effective_user.id)
    first, last = current_activity_period()
    quick_buttons = [InlineKeyboardButton("📍 Hari Ini", callback_data="date:today")]
    if now_jakarta().day > 1:
        quick_buttons.append(InlineKeyboardButton("↩️ Kemarin", callback_data="date:yesterday"))
    kb = InlineKeyboardMarkup([
        quick_buttons,
        [InlineKeyboardButton("❌ Batal", callback_data="date:cancel")]
    ])
    await message.reply_text(
        f"📅 *PILIH TANGGAL AKTIVITAS*\n\n"
        f"Periode aktif: *{first:%d/%m/%Y}–{last:%d/%m/%Y}*.\n"
        "Tanggal sebelum maupun sesudah hari ini diperbolehkan selama masih dalam bulan tersebut.\n\n"
        "Pilih tombol cepat atau ketik DD/MM/YYYY.",
        parse_mode="Markdown", reply_markup=kb)
    return DATE


@private
async def resume_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lanjutkan dari field terakhir tanpa menyimpan cookie/OTP di draft."""
    query = update.callback_query
    if query:
        await query.answer()
    message = update.effective_message
    client = get_client(update.effective_user.id)
    if not await asyncio.to_thread(client.is_authenticated):
        await message.reply_text(
            "🔐 Login OTP diperlukan sebelum melanjutkan draft.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔐 Login OTP", callback_data="menu:login")]]))
        return ConversationHandler.END
    payload = restore_draft(update.effective_user.id, context)
    if not payload:
        await message.reply_text(
            "ℹ️ Draft tidak ditemukan.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "➕ Tambah Aktivitas", callback_data="menu:newadd")]]))
        return ConversationHandler.END
    date_text = context.user_data.get("date")
    try:
        date = parse_activity_date(date_text) if date_text else None
    except ValueError:
        storage.delete_draft(update.effective_user.id)
        context.user_data.clear()
        await message.reply_text(
            "⚠️ Tanggal draft berada di luar bulan berjalan. Mulai aktivitas baru.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🆕 Mulai Baru", callback_data="menu:newadd")]]))
        return ConversationHandler.END
    if not date:
        storage.delete_draft(update.effective_user.id)
        await message.reply_text(
            "⚠️ Draft belum memiliki tanggal. Mulai pengisian baru.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🆕 Mulai Baru", callback_data="menu:newadd")]]))
        return ConversationHandler.END
    if not isinstance(context.user_data.get("target"), WorkTarget):
        await message.reply_text(f"📝 Melanjutkan draft tanggal {date:%d/%m/%Y}…")
        return await load_targets(update, context, date)
    if not isinstance(context.user_data.get("item"), KamusItem):
        await message.reply_text("📝 Draft dipulihkan sampai pilihan tugas jabatan.")
        await prompt_activity_search(message, update.effective_user.id)
        return SEARCH
    item: KamusItem = context.user_data["item"]
    if not isinstance(context.user_data.get("volume"), int):
        await message.reply_text(
            f"📝 Draft dipulihkan\n{item.code} — {item.activity}\n\n"
            "🔢 Masukkan volume (angka bulat):")
        return VOLUME
    if not context.user_data.get("object"):
        await message.reply_text(
            f"📝 Draft dipulihkan\n🔢 Volume: {context.user_data['volume']}\n\n"
            f"Masukkan objek kerja/topik.\nContoh: {item.object_hint}")
        return OBJECT
    await message.reply_text("📝 Draft lengkap dipulihkan. Memeriksa data e‑Master…")
    return await prepare_add_confirmation(message, context, update.effective_user.id)


@private
async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        date = parse_activity_date(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(
            f"⚠️ {exc}\nContoh format: `18/07/2026`", parse_mode="Markdown")
        return DATE
    return await load_targets(update, context, date)


@private
async def quick_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    if action == "cancel":
        storage.delete_draft(update.effective_user.id)
        await query.edit_message_text("❌ Pengisian dibatalkan.")
        return ConversationHandler.END
    moment = now_jakarta()
    date = moment if action == "today" else moment - timedelta(days=1)
    try:
        date = parse_activity_date(date.strftime("%d/%m/%Y"))
    except ValueError as exc:
        await query.edit_message_text(f"⚠️ {exc}\nKetik tanggal lain dalam format DD/MM/YYYY.")
        return DATE
    await query.edit_message_text(f"📅 Tanggal dipilih: *{date:%d/%m/%Y}*", parse_mode="Markdown")
    return await load_targets(update, context, date)


async def load_targets(update: Update, context: ContextTypes.DEFAULT_TYPE, date: datetime):
    message = update.effective_message
    client = get_client(update.effective_user.id)
    context.user_data["date"] = date.strftime("%d/%m/%Y")
    persist_draft(update.effective_user.id, context, "TARGET")
    try:
        targets = await asyncio.to_thread(client.list_work_targets, date.strftime("%m"))
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
    persist_draft(update.effective_user.id, context, "SEARCH")
    await prompt_activity_search(query.message, update.effective_user.id)
    return SEARCH


async def prompt_activity_search(message, telegram_id: int):
    try:
        favorites = storage.list_favorites(telegram_id, 5)
    except sqlite3.DatabaseError:
        # Favorit adalah pintasan opsional. Kerusakan/migrasi data lama tidak
        # boleh menghentikan alur inti Tambah Aktivitas.
        logging.exception("Favorit belum dapat dibaca; alur pencarian tetap dilanjutkan")
        favorites = []
    buttons = [[InlineKeyboardButton(f"⭐ {row[2][:45]} ({row[4]} mnt)",
                                     callback_data=f"favorite:pick:{row[0]}")]
               for row in favorites]
    text = ("🔎 CARI AKTIVITAS\n"
            "Alur: Tanggal → Tugas Jabatan → Aktivitas\n\n"
            "Ketik kata kunci, misalnya: video, rapat, surat, atau dokumen.")
    if favorites:
        text += "\n\nAtau pilih aktivitas favorit:"
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


@private
async def pick_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        favorite_id = int(query.data.rsplit(":", 1)[1])
        row = storage.get_favorite(update.effective_user.id, favorite_id)
        if not row:
            raise ValueError
        item = KamusItem(code=row[1], activity=row[2], unit=row[3], wpt=int(row[4]),
                         description="", object_hint=row[2])
    except (TypeError, ValueError):
        await query.edit_message_text("⚠️ Favorit tidak ditemukan. Ketik kata kunci aktivitas.")
        return SEARCH
    context.user_data["item"] = item
    persist_draft(update.effective_user.id, context, "VOLUME")
    await query.edit_message_text(
        f"⭐ FAVORIT DIPILIH\n\n{item.code} — {item.activity}\n📦 {item.unit}\n⏱ {item.wpt} menit")
    await query.message.reply_text("🔢 Masukkan volume (angka bulat):")
    return VOLUME


@private
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = get_client(update.effective_user.id)
    keyword = update.message.text.strip()
    if len(keyword) > 100:
        await update.message.reply_text("Kata kunci terlalu panjang. Maksimal 100 karakter.")
        return SEARCH
    try:
        items = await asyncio.to_thread(client.search_kamus, keyword)
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
    context.user_data["kamus_keyword"] = keyword
    text, markup = build_kamus_page(items, keyword, 0)
    await update.message.reply_text(text, reply_markup=markup)
    return PICK


def build_kamus_page(items: list[KamusItem], keyword: str, requested_page: int):
    """Buat satu halaman tombol; indeks callback tetap menunjuk daftar lengkap."""
    total = len(items)
    page_count = max(1, (total + KAMUS_PAGE_SIZE - 1) // KAMUS_PAGE_SIZE)
    page = min(max(0, requested_page), page_count - 1)
    start = page * KAMUS_PAGE_SIZE
    end = min(total, start + KAMUS_PAGE_SIZE)
    buttons = [[InlineKeyboardButton(
        f"{item.code} — {item.activity[:46]} ({item.wpt} mnt)",
        callback_data=f"pick:{index}")]
        for index, item in enumerate(items[start:end], start=start)]

    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"kamus:page:{page-1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("Berikutnya ➡️", callback_data=f"kamus:page:{page+1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton("🔎 Ganti kata kunci", callback_data="kamus:search")])
    text = (f"📚 HASIL KAMUS AKTIVITAS\n"
            f"Kata kunci: {keyword}\n"
            f"Menampilkan {start + 1}–{end} dari {total} hasil "
            f"(halaman {page + 1}/{page_count}).\n\nPilih salah satu:")
    return text, InlineKeyboardMarkup(buttons)


@private
async def navigate_kamus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "kamus:search":
        await query.edit_message_text("🔎 CARI AKTIVITAS\nKetik kata kunci baru, misalnya: surat, video, atau rapat.")
        return SEARCH
    try:
        page = int(query.data.rsplit(":", 1)[1])
        items: list[KamusItem] = context.user_data["items"]
        keyword = context.user_data["kamus_keyword"]
    except (KeyError, TypeError, ValueError):
        await query.edit_message_text("⚠️ Hasil pencarian sudah kedaluwarsa. Tekan /tambah untuk memulai kembali.")
        return ConversationHandler.END
    text, markup = build_kamus_page(items, keyword, page)
    await query.edit_message_text(text, reply_markup=markup)
    return PICK


@private
async def pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        idx = int(query.data.split(":")[1])
        item: KamusItem = context.user_data["items"][idx]
    except (KeyError, IndexError, TypeError, ValueError):
        await query.edit_message_text("⚠️ Hasil pencarian sudah kedaluwarsa. Tekan /tambah untuk memulai kembali.")
        return ConversationHandler.END
    context.user_data["item"] = item
    persist_draft(update.effective_user.id, context, "VOLUME")
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
    persist_draft(update.effective_user.id, context, "OBJECT")
    await update.message.reply_text(f"📝 Masukkan objek kerja/topik.\nContoh dari kamus: {item.object_hint}")
    return OBJECT


@private
async def object_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.caption if update.message.photo else update.message.text) or ""
    text = tidy_sentence(text)
    if update.message.photo and not text:
        await update.message.reply_text(
            "⚠️ Foto harus disertai caption berisi uraian aktivitas.")
        return OBJECT
    if len(text) < 5:
        await update.message.reply_text("Objek kerja terlalu pendek.")
        return OBJECT
    if len(text) > 1000:
        await update.message.reply_text("Objek kerja terlalu panjang. Maksimal 1.000 karakter.")
        return OBJECT
    context.user_data["object"] = text
    context.user_data["source_chat_id"] = update.effective_chat.id
    context.user_data["source_message_id"] = update.message.message_id
    if update.message.photo:
        photo = update.message.photo[-1]
        context.user_data["photos"] = [{
            "file_id": photo.file_id,
            "unique_id": photo.file_unique_id,
            "mime_type": "image/jpeg",
            "extension": ".jpg",
        }]
    persist_draft(update.effective_user.id, context, "CONFIRM")
    return await prepare_add_confirmation(update.effective_message, context, update.effective_user.id)


async def prepare_add_confirmation(message, context: ContextTypes.DEFAULT_TYPE, telegram_id: int,
                                   volume_state: int = VOLUME):
    item = context.user_data["item"]
    vol = context.user_data["volume"]
    text = context.user_data["object"]
    date = context.user_data["date"]
    try:
        client = get_client(telegram_id)
        existing_minutes, duplicates = await day_assessment(
            client, date, item.activity, text)
    except AuthenticationRequired:
        await message.reply_text(
            "🔐 Sesi e‑Master habis. Draft sudah disimpan. Login OTP lalu tekan Lanjutkan Draft.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Login OTP", callback_data="menu:login")]]))
        return ConversationHandler.END
    except EMasterError as exc:
        await message.reply_text(f"❌ WPT harian belum dapat diperiksa: {exc}\nDraft tetap tersimpan.")
        return ConversationHandler.END
    new_minutes = item.wpt * vol
    if existing_minutes + new_minutes > 660:
        await message.reply_text(
            "❌ TOTAL WPT HARIAN MELEBIHI BATAS\n"
            f"WPT saat ini: {existing_minutes} menit\n"
            f"Aktivitas baru: {new_minutes} menit\n"
            f"Total: {existing_minutes + new_minutes}/660 menit\n\n"
            "Masukkan volume yang lebih kecil.")
        return volume_state
    context.user_data["duplicate_override"] = bool(duplicates)
    summary = (f"📋 KONFIRMASI AKTIVITAS\n━━━━━━━━━━━━━━━━━━━━\n"
               f"📅 Tanggal: {date}\n\n"
               f"🎯 Tugas Jabatan:\n{context.user_data['target'].name}\n\n"
               f"📌 Aktivitas:\n{item.code} — {item.activity}\n\n"
               f"📦 Satuan: {item.unit}\n⏱ WPT aktivitas: {item.wpt} × {vol} = {new_minutes} menit\n"
               f"📊 WPT hari ini: {existing_minutes} → {existing_minutes + new_minutes}/660 menit\n\n"
               f"📝 Objek Kerja:\n{text}")
    photos = context.user_data.get("photos") or []
    if photos:
        summary += f"\n\n📷 Foto: {len(photos)} bukti dukung"
    if duplicates:
        summary = ("⚠️ AKTIVITAS MIRIP SUDAH ADA\n"
                   "Tanggal, aktivitas, dan objek kerja sama dengan data e‑Master.\n\n" + summary)
    send_callback = "send_duplicate" if duplicates else "send"
    send_label = "⚠️ Tetap Kirim" if duplicates else "✅ Kirim ke e‑Master"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(send_label, callback_data=send_callback),
         InlineKeyboardButton("❌ Batal", callback_data="cancel")],
        [InlineKeyboardButton("📷 Tambah Foto", callback_data="add_photo")],
    ])
    # Teks dari e-Master dapat berisi underscore seperti eastjavatrip_id.
    # Jangan gunakan Markdown agar seluruh karakter selalu aman ditampilkan.
    await message.reply_text(summary, reply_markup=kb)
    return CONFIRM


async def upload_activity_photos(context: ContextTypes.DEFAULT_TYPE, user, activity_id: int,
                                 activity_date: str, activity_time: str):
    photos = context.user_data.get("photos") or []
    if not isinstance(photos, list) or not photos:
        return True, [], []
    created_at = now_jakarta().isoformat(timespec="seconds")
    employee_folder_id = None
    day_folder_id = None
    failed_ids = []
    errors = []
    evidence_ids = []
    parsed_date = datetime.strptime(activity_date, "%Y-%m-%d")
    for index, photo in enumerate(photos, start=1):
        file_name = photo.get("file_name") or (
            f"{activity_date}_{activity_time.replace(':', '.')}_"
            f"{activity_id:06d}_{index:02d}{photo.get('extension', '.jpg')}")
        evidence_id = storage.add_evidence(
            daily_activity_id=activity_id,
            telegram_file_id=photo["file_id"],
            telegram_unique_id=photo["unique_id"],
            mime_type=photo.get("mime_type", "image/jpeg"),
            file_name=file_name,
            created_at_local=created_at,
        )
        evidence_ids.append(evidence_id)
        existing = storage.get_evidence(evidence_id)
        if existing and existing[8] == "uploaded":
            continue
        try:
            telegram_file = await context.bot.get_file(photo["file_id"])
            content = bytes(await telegram_file.download_as_bytearray())
            if not employee_folder_id:
                employee_folder_id = await asyncio.to_thread(
                    drive_storage.employee_folder, user[3] or str(user[0]), user[10])
                if not user[10]:
                    storage.set_drive_folder_id(user[0], employee_folder_id)
            if not day_folder_id:
                day_folder_id = await asyncio.to_thread(
                    drive_storage.day_folder, employee_folder_id,
                    parsed_date.year, parsed_date.month, parsed_date.day)
            file_id, file_url = await asyncio.to_thread(
                drive_storage.upload_bytes,
                content=content, file_name=file_name,
                mime_type=photo.get("mime_type", "image/jpeg"), parent_id=day_folder_id)
            storage.mark_evidence_uploaded(evidence_id, file_id, file_url, file_name)
        except Exception as exc:
            message = str(exc) if isinstance(exc, DriveStorageError) else "Bukti foto belum dapat diunggah."
            storage.mark_evidence_failed(evidence_id, message)
            failed_ids.append(evidence_id)
            errors.append(message)
            logging.warning("Upload bukti gagal untuk activity_id=%s: %s",
                            activity_id, type(exc).__name__)
    return not failed_ids, failed_ids, errors


async def show_daily_confirmation(message, context: ContextTypes.DEFAULT_TYPE, *, edit=False):
    payload = context.user_data["daily_entry"]
    parsed_date = datetime.strptime(payload["activity_date"], "%Y-%m-%d").date()
    photos = context.user_data.get("photos") or []
    photo_note = f"{len(photos)} foto" if photos else "Tanpa foto"
    text = (
        "📋 KONFIRMASI AKTIVITAS\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Tanggal: {format_indonesian_date(parsed_date)}\n"
        f"🕘 Waktu: {payload['activity_time']} WIB\n"
        f"📷 Bukti: {photo_note}\n\n"
        f"📝 Aktivitas:\n{payload['text']}")
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Simpan", callback_data="daily:save"),
         InlineKeyboardButton("✏️ Ubah", callback_data="daily:edit")],
        [InlineKeyboardButton("📷 Tambah Foto", callback_data="daily:addphoto")],
        [InlineKeyboardButton("❌ Batalkan", callback_data="daily:cancel")],
    ])
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


@private
async def daily_activity_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = (message.caption if message.photo else message.text) or ""
    text = tidy_sentence(text)
    if message.photo and not text:
        await message.reply_text("⚠️ Foto harus disertai caption berisi uraian aktivitas.")
        return DAILY_ACTIVITY
    if len(text) < 10:
        await message.reply_text(
            "⚠️ Uraian terlalu pendek. Tuliskan kegiatan dan hasilnya minimal 10 karakter.")
        return DAILY_ACTIVITY
    if len(text) > 2000:
        await message.reply_text("⚠️ Uraian maksimal 2.000 karakter.")
        return DAILY_ACTIVITY
    moment = now_jakarta()
    context.user_data["daily_entry"] = {
        "activity_date": moment.date().isoformat(),
        "activity_time": moment.strftime("%H:%M:%S"),
        "created_at_local": moment.isoformat(timespec="seconds"),
        "text": text,
        "source_chat_id": update.effective_chat.id,
        "source_message_id": message.message_id,
    }
    if message.photo:
        photo = message.photo[-1]
        context.user_data["photos"] = [{
            "file_id": photo.file_id,
            "unique_id": photo.file_unique_id,
            "mime_type": "image/jpeg",
            "extension": ".jpg",
        }]
    await show_daily_confirmation(message, context)
    return DAILY_CONFIRM


@private
async def daily_activity_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.rsplit(":", 1)[1]
    if action == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Aktivitas dibatalkan dan tidak disimpan.")
        return ConversationHandler.END
    if action == "edit":
        await query.edit_message_text(
            "✏️ Kirim ulang uraian aktivitas, atau kirim ulang foto beserta caption yang benar.")
        return DAILY_ACTIVITY
    if action == "addphoto":
        context.user_data["photo_return"] = "daily"
        await query.edit_message_text(
            "📷 Kirim foto tambahan. Caption tidak wajib karena uraian aktivitas sudah tersimpan.\n\n"
            "Anda dapat mengulangi tombol Tambah Foto untuk menambahkan beberapa bukti.")
        return ADDITIONAL_PHOTO
    payload = context.user_data.get("daily_entry")
    user = storage.get_user(update.effective_user.id)
    if not isinstance(payload, dict):
        await query.edit_message_text("⚠️ Data konfirmasi sudah kedaluwarsa. Mulai ulang dari menu.")
        return ConversationHandler.END
    await query.edit_message_text("⏳ Menyimpan aktivitas…")
    activity_id, created = storage.add_daily_activity(
        telegram_id=user[0], employee_type="non_asn",
        activity_date=payload["activity_date"], activity_time=payload["activity_time"],
        activity_text=payload["text"], created_at_local=payload["created_at_local"],
        emaster_status="not_required", source_chat_id=payload["source_chat_id"],
        source_message_id=payload["source_message_id"],
    )
    uploaded, failed_ids, _ = await upload_activity_photos(
        context, user, activity_id, payload["activity_date"], payload["activity_time"])
    buttons = [[InlineKeyboardButton("➕ Tambah Lagi", callback_data="menu:add"),
                InlineKeyboardButton("📋 Hari Ini", callback_data="daily:today")],
               [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")]]
    note = ""
    if context.user_data.get("photos") and not uploaded:
        note = "\n\n⚠️ Aktivitas tersimpan, tetapi ada foto yang belum berhasil diproses."
        for evidence_id in reversed(failed_ids):
            buttons.insert(0, [InlineKeyboardButton(
                "🔄 Coba Lagi Foto", callback_data=f"evidence:retry:{evidence_id}")])
    duplicate_note = "\nℹ️ Pesan ini sebelumnya sudah disimpan." if not created else ""
    await query.edit_message_text(
        f"✅ AKTIVITAS BERHASIL DISIMPAN\n\n{payload['text']}{note}{duplicate_note}",
        reply_markup=InlineKeyboardMarkup(buttons))
    context.user_data.clear()
    return ConversationHandler.END


@private
async def additional_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("⚠️ Kirim file sebagai foto, bukan pesan teks atau dokumen.")
        return ADDITIONAL_PHOTO
    photos = context.user_data.setdefault("photos", [])
    photo = update.message.photo[-1]
    if any(item.get("unique_id") == photo.file_unique_id for item in photos):
        await update.message.reply_text("ℹ️ Foto tersebut sudah ditambahkan.")
    elif len(photos) >= 5:
        await update.message.reply_text("⚠️ Maksimal 5 foto untuk satu aktivitas.")
    else:
        photos.append({
            "file_id": photo.file_id,
            "unique_id": photo.file_unique_id,
            "mime_type": "image/jpeg",
            "extension": ".jpg",
        })
    return_to = context.user_data.pop("photo_return", "daily")
    if return_to in {"asn", "copy"}:
        persist_draft(update.effective_user.id, context, "CONFIRM")
        return await prepare_add_confirmation(
            update.effective_message, context, update.effective_user.id,
            volume_state=COPY_VOLUME if return_to == "copy" else VOLUME)
    await show_daily_confirmation(update.effective_message, context)
    return DAILY_CONFIRM


@private
async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        storage.delete_draft(update.effective_user.id)
        await query.edit_message_text("Pengisian dibatalkan.")
        return ConversationHandler.END
    if query.data == "add_photo":
        context.user_data["photo_return"] = (
            "copy" if isinstance(context.user_data.get("copy_source"), EMasterActivity)
            else "asn")
        await query.edit_message_text(
            "📷 Kirim foto tambahan. Caption tidak wajib karena uraian aktivitas sudah tersimpan.\n\n"
            "Maksimal 5 foto untuk satu aktivitas.")
        return ADDITIONAL_PHOTO
    item = context.user_data["item"]
    date = context.user_data["date"]
    allow_duplicate = query.data == "send_duplicate"
    try:
        client = get_client(update.effective_user.id)
        await query.edit_message_text("⏳ Memeriksa ulang WPT dan mengirim ke e‑Master…")
        existing_minutes, duplicates = await day_assessment(
            client, date, item.activity, context.user_data["object"])
        new_minutes = item.wpt * context.user_data["volume"]
        if existing_minutes + new_minutes > 660:
            await query.edit_message_text(
                f"❌ Total WPT berubah menjadi {existing_minutes + new_minutes}/660 menit. "
                "Draft tidak dikirim; lanjutkan draft dan kurangi volume.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Lanjutkan Draft", callback_data="menu:resume")]]))
            return ConversationHandler.END
        if duplicates and not allow_duplicate:
            await query.edit_message_text(
                "⚠️ Aktivitas serupa ditemukan saat pemeriksaan terakhir. Buka Lanjutkan Draft untuk meninjau ulang.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Lanjutkan Draft", callback_data="menu:resume")]]))
            return ConversationHandler.END
        await asyncio.to_thread(
            client.submit_activity,
            month=date[3:5], target=context.user_data["target"], date=date, item=item,
            volume=context.user_data["volume"], object_work=context.user_data["object"])
        storage.add_sent(update.effective_user.id, date, item, context.user_data["volume"], context.user_data["object"])
        moment = now_jakarta()
        activity_date_iso_value = activity_date_iso(date)
        activity_time = moment.strftime("%H:%M:%S")
        user = storage.get_user(update.effective_user.id)
        activity_id, _ = storage.add_daily_activity(
            telegram_id=update.effective_user.id, employee_type="asn",
            activity_date=activity_date_iso_value, activity_time=activity_time,
            activity_text=context.user_data["object"],
            created_at_local=moment.isoformat(timespec="seconds"),
            emaster_status="sent", emaster_code=item.code,
            emaster_activity=item.activity,
            emaster_target=context.user_data["target"].name,
            wpt_minutes=item.wpt * context.user_data["volume"],
            source_chat_id=context.user_data.get("source_chat_id"),
            source_message_id=context.user_data.get("source_message_id"),
        )
        uploaded, failed_ids, _ = await upload_activity_photos(
            context, user, activity_id, activity_date_iso_value, activity_time)
        storage.delete_draft(update.effective_user.id)
        context.user_data["last_sent"] = item
        buttons = [[InlineKeyboardButton("⭐ Simpan Favorit", callback_data=f"favorite:add:{item.code}")],
                   [InlineKeyboardButton("➕ Tambah Lagi", callback_data="menu:add"),
                    InlineKeyboardButton("📊 Dashboard", callback_data="menu:progress")],
                   [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")]]
        photo_note = ""
        if context.user_data.get("photos") and not uploaded:
            photo_note = "\n\n⚠️ Aktivitas sudah masuk E-Master, tetapi ada foto yang belum berhasil diproses."
            for evidence_id in reversed(failed_ids):
                buttons.insert(0, [InlineKeyboardButton(
                    "🔄 Coba Lagi Foto", callback_data=f"evidence:retry:{evidence_id}")])
        kb = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(f"✅ BERHASIL TERSIMPAN\n\n{item.activity}\n⏱ {item.wpt * context.user_data['volume']} menit\n📅 {date}{photo_note}",
                                      reply_markup=kb)
    except AuthenticationRequired:
        await query.edit_message_text("🔐 Sesi habis. Jalankan /login, lalu ulangi pengiriman.")
    except (EMasterError, KeyError) as exc:
        await query.edit_message_text(f"❌ Gagal mengirim: {exc}")
    return ConversationHandler.END


@private
async def add_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    item = context.user_data.get("last_sent")
    code = query.data.rsplit(":", 1)[1]
    if not isinstance(item, KamusItem) or item.code != code:
        await query.answer("Data sudah kedaluwarsa", show_alert=True)
        await query.edit_message_text("⚠️ Data aktivitas sudah kedaluwarsa. Simpan favorit dari pengiriman berikutnya.")
        return
    storage.add_favorite(update.effective_user.id, item)
    await query.answer("Favorit tersimpan ⭐", show_alert=True)
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Tersimpan di Favorit", callback_data="menu:favorites")],
            [InlineKeyboardButton("➕ Tambah Lagi", callback_data="menu:add"),
             InlineKeyboardButton("📊 Dashboard", callback_data="menu:progress")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")],
        ]))
    except Exception:
        logging.debug("Markup favorit tidak dapat diperbarui")


@private
async def favorites_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rows = storage.list_favorites(update.effective_user.id, 20)
    buttons = [[InlineKeyboardButton(f"🗑 {row[2][:44]}", callback_data=f"favorite:remove:{row[0]}")]
               for row in rows]
    buttons.append([InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")])
    text = "⭐ FAVORIT SAYA\n━━━━━━━━━━━━━━━━━━━━\n"
    text += ("Pilih tombol 🗑 untuk menghapus favorit.\n\n" +
             "\n".join(f"{index+1}. {row[2]} · {row[4]} menit" for index, row in enumerate(rows))) if rows \
            else "Belum ada favorit. Simpan setelah aktivitas berhasil dikirim."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


@private
async def remove_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Favorit dihapus")
    try:
        favorite_id = int(query.data.rsplit(":", 1)[1])
    except ValueError:
        return
    storage.delete_favorite(update.effective_user.id, favorite_id)
    rows = storage.list_favorites(update.effective_user.id, 20)
    buttons = [[InlineKeyboardButton(f"🗑 {row[2][:44]}", callback_data=f"favorite:remove:{row[0]}")]
               for row in rows]
    buttons.append([InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")])
    text = "⭐ FAVORIT SAYA\n━━━━━━━━━━━━━━━━━━━━\n"
    text += ("Pilih tombol 🗑 untuk menghapus favorit.\n\n" +
             "\n".join(f"{index+1}. {row[2]} · {row[4]} menit" for index, row in enumerate(rows))) if rows \
            else "Belum ada favorit."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


@private
async def progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    message = update.effective_message
    now = now_jakarta()
    user = storage.get_user(update.effective_user.id)
    if employee_type(user) == "non_asn":
        month_prefix = now_jakarta().strftime("%Y-%m")
        rows = storage.db.execute("""SELECT COUNT(*),COUNT(DISTINCT activity_date)
          FROM daily_activities WHERE telegram_id=? AND status='active'
          AND substr(activity_date,1,7)=?""", (user[0], month_prefix)).fetchone()
        await message.reply_text(
            "📊 RINGKASAN AKTIVITAS\n━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 Aktivitas bulan ini: {rows[0]}\n"
            f"📅 Hari aktif: {rows[1]}\n\n"
            "Pegawai Non-ASN tidak memiliki perhitungan WPT E-Master.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🏠 Menu Utama", callback_data="menu:home")]]))
        return
    try:
        client = get_client(update.effective_user.id)
        current = await asyncio.to_thread(client.get_month_progress, now.strftime("%m"))
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
    user = storage.get_user(update.effective_user.id)
    if employee_type(user) == "non_asn":
        return await daily_history(update, context)
    if update.callback_query:
        await update.callback_query.answer()
    message = update.effective_message
    status = await message.reply_text("⏳ Mengambil riwayat terbaru langsung dari e‑Master…")
    try:
        client = get_client(update.effective_user.id)
        rows = await asyncio.to_thread(
            client.list_activities, now_jakarta().strftime("%m"), 200)
    except AuthenticationRequired:
        await status.edit_text("🔐 Sesi e‑Master habis. Jalankan /login, lalu buka Riwayat kembali.")
        return
    except EMasterError as exc:
        await status.edit_text(f"❌ Tidak dapat mengambil riwayat: {exc}")
        return
    if not rows:
        await status.edit_text("🕘 Belum ada aktivitas pada e‑Master bulan ini.")
        return
    context.user_data["history_items"] = rows
    text, markup = build_history_page(rows, 0)
    await status.edit_text(text, reply_markup=markup)


def build_history_page(rows: list[EMasterActivity], requested_page: int):
    total = len(rows)
    page_count = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = min(max(0, requested_page), page_count - 1)
    start = page * HISTORY_PAGE_SIZE
    end = min(total, start + HISTORY_PAGE_SIZE)
    lines = [f"🕘 RIWAYAT E‑MASTER — {now_jakarta():%m/%Y}", "━━━━━━━━━━━━━━━━━━━━"]
    buttons = []
    for index, item in enumerate(rows[start:end], start=start):
        detail = item.detail if len(item.detail) <= 80 else item.detail[:77] + "…"
        object_work = item.object_work if len(item.object_work) <= 100 else item.object_work[:97] + "…"
        lines.append(f"\n{index + 1}. 📅 {item.date} · {item.total_minutes} menit\n{detail}\n{object_work}")
        buttons.append([
            InlineKeyboardButton(f"✏️ Edit {index + 1}", callback_data=f"edit:pick:{index}"),
            InlineKeyboardButton(f"📋 Salin {index + 1}", callback_data=f"copy:pick:{index}"),
            InlineKeyboardButton(f"🗑 Hapus {index + 1}", callback_data=f"delete:pick:{index}"),
        ])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"history:page:{page-1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("Berikutnya ➡️", callback_data=f"history:page:{page+1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton("🔄 Perbarui", callback_data="menu:history"),
                    InlineKeyboardButton("🏠 Menu", callback_data="menu:home")])
    lines.insert(2, f"Menampilkan {start + 1}–{end} dari {total} aktivitas (halaman {page + 1}/{page_count}).")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


@private
async def history_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        page = int(query.data.rsplit(":", 1)[1])
        rows: list[EMasterActivity] = context.user_data["history_items"]
    except (KeyError, TypeError, ValueError):
        await query.edit_message_text("⚠️ Riwayat sudah kedaluwarsa. Buka /riwayat kembali.")
        return
    text, markup = build_history_page(rows, page)
    await query.edit_message_text(text, reply_markup=markup)


def _history_item(context: ContextTypes.DEFAULT_TYPE, callback_data: str) -> EMasterActivity:
    index = int(callback_data.rsplit(":", 1)[1])
    return context.user_data["history_items"][index]


def build_edit_kamus_page(items: list[KamusItem], keyword: str, requested_page: int):
    total = len(items)
    page_count = max(1, (total + KAMUS_PAGE_SIZE - 1) // KAMUS_PAGE_SIZE)
    page = min(max(requested_page, 0), page_count - 1)
    start = page * KAMUS_PAGE_SIZE
    end = min(total, start + KAMUS_PAGE_SIZE)
    buttons = [[InlineKeyboardButton(
        f"{item.code} — {item.activity[:44]} ({item.wpt} mnt)",
        callback_data=f"edititem:pick:{index}")]
        for index, item in enumerate(items[start:end], start=start)]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"editkamus:page:{page-1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("Berikutnya ➡️", callback_data=f"editkamus:page:{page+1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton("🔎 Ganti kata kunci", callback_data="editkamus:search")])
    text = ("📚 PILIH AKTIVITAS PENGGANTI\n"
            f"Kata kunci: {keyword}\n"
            f"Menampilkan {start + 1}–{end} dari {total} "
            f"(halaman {page + 1}/{page_count}).")
    return text, InlineKeyboardMarkup(buttons)


async def _prompt_edit_date(message, original: EMasterActivity):
    await message.reply_text(
        "📅 TANGGAL AKTIVITAS\n"
        f"Saat ini: {original.date.replace('-', '/')}\n\n"
        "Ketik tanggal baru (DD/MM/YYYY) atau '-' untuk mempertahankan.")


@private
async def edit_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        original = _history_item(context, query.data)
    except (KeyError, IndexError, TypeError, ValueError):
        await query.edit_message_text("⚠️ Riwayat sudah kedaluwarsa. Buka Riwayat kembali.")
        return ConversationHandler.END
    context.user_data["edit_original"] = original
    context.user_data.pop("edit_item", None)
    await query.edit_message_text(
        "✏️ EDIT AKTIVITAS\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {original.detail}\n📅 {original.date}\n"
        f"⏱ {original.wpt} × {original.volume} = {original.total_minutes} menit\n\n"
        "Pertahankan aktivitas kamus saat ini atau cari pengganti?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Pertahankan Aktivitas", callback_data="edit:keepactivity")],
            [InlineKeyboardButton("❌ Batal", callback_data="edit:cancel")],
        ]))
    await query.message.reply_text("Untuk mengganti aktivitas, ketik kata kunci kamus.")
    return EDIT_ACTIVITY


@private
async def edit_keep_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "edit:cancel":
        context.user_data.pop("edit_original", None)
        await query.edit_message_text("✅ Perubahan dibatalkan. Data e‑Master tidak berubah.")
        return ConversationHandler.END
    original = context.user_data.get("edit_original")
    if not isinstance(original, EMasterActivity):
        await query.edit_message_text("⚠️ Data edit sudah kedaluwarsa. Buka Riwayat kembali.")
        return ConversationHandler.END
    context.user_data.pop("edit_item", None)
    await query.edit_message_text("✅ Aktivitas kamus dipertahankan.")
    await _prompt_edit_date(query.message, original)
    return EDIT_DATE


@private
async def edit_search_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text.strip()
    if not 1 <= len(keyword) <= 100:
        await update.message.reply_text("Kata kunci harus 1–100 karakter.")
        return EDIT_ACTIVITY
    try:
        items = await asyncio.to_thread(get_client(update.effective_user.id).search_kamus, keyword)
    except AuthenticationRequired:
        await update.message.reply_text("🔐 Sesi habis. Login OTP lalu buka Riwayat kembali.")
        return ConversationHandler.END
    except EMasterError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return EDIT_ACTIVITY
    if not items:
        await update.message.reply_text("Aktivitas tidak ditemukan. Coba kata kunci lain.")
        return EDIT_ACTIVITY
    context.user_data["edit_items"] = items
    context.user_data["edit_keyword"] = keyword
    text, markup = build_edit_kamus_page(items, keyword, 0)
    await update.message.reply_text(text, reply_markup=markup)
    return EDIT_PICK


@private
async def edit_navigate_kamus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "editkamus:search":
        await query.edit_message_text("🔎 Ketik kata kunci aktivitas pengganti.")
        return EDIT_ACTIVITY
    try:
        page = int(query.data.rsplit(":", 1)[1])
        items = context.user_data["edit_items"]
        keyword = context.user_data["edit_keyword"]
    except (KeyError, TypeError, ValueError):
        await query.edit_message_text("⚠️ Hasil sudah kedaluwarsa. Buka Riwayat kembali.")
        return ConversationHandler.END
    text, markup = build_edit_kamus_page(items, keyword, page)
    await query.edit_message_text(text, reply_markup=markup)
    return EDIT_PICK


@private
async def edit_pick_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.rsplit(":", 1)[1])
        item = context.user_data["edit_items"][index]
        original = context.user_data["edit_original"]
    except (KeyError, IndexError, TypeError, ValueError):
        await query.edit_message_text("⚠️ Pilihan sudah kedaluwarsa. Buka Riwayat kembali.")
        return ConversationHandler.END
    context.user_data["edit_item"] = item
    await query.edit_message_text(
        f"✅ AKTIVITAS PENGGANTI\n{item.code} — {item.activity}\n"
        f"📦 {item.unit} · ⏱ {item.wpt} menit")
    await _prompt_edit_date(query.message, original)
    return EDIT_DATE


@private
async def edit_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    original = context.user_data.get("edit_original")
    if not isinstance(original, EMasterActivity):
        await update.message.reply_text("⚠️ Data edit sudah kedaluwarsa. Buka Riwayat kembali.")
        return ConversationHandler.END
    raw = update.message.text.strip()
    try:
        date = parse_activity_date(original.date if raw == "-" else raw)
    except ValueError as exc:
        await update.message.reply_text(f"⚠️ {exc}\nKetik DD/MM/YYYY atau `-`.", parse_mode="Markdown")
        return EDIT_DATE
    context.user_data["edit_date"] = date.strftime("%d/%m/%Y")
    await update.message.reply_text(
        f"🔢 VOLUME\nSaat ini: {original.volume}\n\n"
        "Ketik volume baru (1–100) atau `-` untuk mempertahankan.", parse_mode="Markdown")
    return EDIT_VOLUME


@private
async def edit_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    original = context.user_data.get("edit_original")
    if not isinstance(original, EMasterActivity):
        return ConversationHandler.END
    raw = update.message.text.strip()
    try:
        value = original.volume if raw == "-" else int(raw)
        if not 1 <= value <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Volume harus angka bulat 1–100 atau `-`.", parse_mode="Markdown")
        return EDIT_VOLUME
    item = context.user_data.get("edit_item")
    wpt = item.wpt if isinstance(item, KamusItem) else original.wpt
    if wpt * value > 660:
        await update.message.reply_text(
            f"❌ WPT aktivitas {wpt * value} menit melebihi 660. Masukkan volume lebih kecil.")
        return EDIT_VOLUME
    context.user_data["edit_volume"] = value
    await update.message.reply_text(
        "📝 OBJEK KERJA/TOPIK\n"
        f"Saat ini:\n{original.object_work}\n\n"
        "Ketik objek baru atau '-' untuk mempertahankan.")
    return EDIT_OBJECT


@private
async def edit_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    original = context.user_data.get("edit_original")
    if not isinstance(original, EMasterActivity):
        return ConversationHandler.END
    raw = update.message.text.strip()
    object_work = original.object_work if raw == "-" else raw
    if not 5 <= len(object_work) <= 1000:
        await update.message.reply_text("Objek kerja harus 5–1.000 karakter atau ketik `-`.", parse_mode="Markdown")
        return EDIT_OBJECT
    context.user_data["edit_object"] = object_work
    item = context.user_data.get("edit_item")
    activity = item.activity if isinstance(item, KamusItem) else original.detail
    wpt = item.wpt if isinstance(item, KamusItem) else original.wpt
    date = context.user_data["edit_date"]
    volume_value = context.user_data["edit_volume"]
    try:
        minutes, duplicates = await day_assessment(
            get_client(update.effective_user.id), date, activity, object_work,
            exclude_id=original.id_realisasi)
    except AuthenticationRequired:
        await update.message.reply_text("🔐 Sesi habis. Login OTP lalu buka Riwayat kembali.")
        return ConversationHandler.END
    except EMasterError as exc:
        await update.message.reply_text(f"❌ Perubahan belum dapat diperiksa: {exc}")
        return ConversationHandler.END
    total = minutes + wpt * volume_value
    if total > 660:
        await update.message.reply_text(
            f"❌ Total WPT tanggal tersebut menjadi {total}/660 menit. Masukkan volume lebih kecil.")
        return EDIT_VOLUME
    warning = ("\n\n⚠️ Aktivitas dan objek yang sama juga ditemukan pada tanggal ini."
               if duplicates else "")
    await update.message.reply_text(
        "✏️ KONFIRMASI PERUBAHAN\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {original.date} → {date}\n"
        f"📌 {original.detail} → {activity}\n"
        f"⏱ {original.total_minutes} → {wpt * volume_value} menit\n"
        f"📊 Total WPT hari itu: {total}/660 menit\n\n"
        f"📝 {object_work}{warning}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Simpan Perubahan", callback_data=f"edit:save:{original.id_realisasi}")],
            [InlineKeyboardButton("❌ Batal", callback_data="edit:cancel")],
        ]))
    return EDIT_CONFIRM


@private
async def edit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "edit:cancel":
        await query.edit_message_text("✅ Perubahan dibatalkan. Data e‑Master tidak berubah.")
        return ConversationHandler.END
    try:
        original: EMasterActivity = context.user_data["edit_original"]
        requested_id = query.data.rsplit(":", 1)[1]
        if requested_id != original.id_realisasi:
            raise ValueError
        item = context.user_data.get("edit_item")
        activity = item.activity if isinstance(item, KamusItem) else original.detail
        wpt = item.wpt if isinstance(item, KamusItem) else original.wpt
        date = context.user_data["edit_date"]
        volume_value = context.user_data["edit_volume"]
        object_work = context.user_data["edit_object"]
    except (KeyError, TypeError, ValueError):
        await query.edit_message_text("⚠️ Konfirmasi edit sudah kedaluwarsa. Buka Riwayat kembali.")
        return ConversationHandler.END
    await query.edit_message_text("⏳ Memeriksa ulang dan menyimpan perubahan ke e‑Master…")
    try:
        client = get_client(update.effective_user.id)
        minutes, _ = await day_assessment(
            client, date, activity, object_work, exclude_id=original.id_realisasi)
        if minutes + wpt * volume_value > 660:
            raise EMasterError("Total WPT harian berubah dan sekarang melebihi 660 menit.")
        updated = await asyncio.to_thread(
            client.update_activity, original, date=date, volume=volume_value,
            object_work=object_work, item=item if isinstance(item, KamusItem) else None)
        storage.add_edited(update.effective_user.id, original, updated)
        storage.sync_daily_edit(
            update.effective_user.id,
            before_date=activity_date_iso(original.date),
            before_activity=original.detail, before_text=original.object_work,
            after_date=activity_date_iso(updated.date),
            after_activity=updated.detail, after_text=updated.object_work,
            after_wpt_minutes=updated.total_minutes)
    except AuthenticationRequired:
        await query.edit_message_text("🔐 Sesi habis. Login OTP lalu buka Riwayat kembali.")
        return ConversationHandler.END
    except EMasterError as exc:
        await query.edit_message_text(f"❌ Aktivitas belum diubah: {exc}")
        return ConversationHandler.END
    context.user_data.pop("history_items", None)
    await query.edit_message_text(
        f"✅ AKTIVITAS BERHASIL DIPERBARUI\n\n{updated.detail}\n"
        f"📅 {updated.date} · ⏱ {updated.total_minutes} menit",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Buka Riwayat", callback_data="menu:history"),
             InlineKeyboardButton("📊 Dashboard", callback_data="menu:progress")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")],
        ]))
    return ConversationHandler.END


@private
async def copy_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        source = _history_item(context, query.data)
    except (KeyError, IndexError, TypeError, ValueError):
        await query.edit_message_text("⚠️ Riwayat sudah kedaluwarsa. Buka Riwayat kembali.")
        return ConversationHandler.END
    context.user_data["copy_source"] = source
    await query.edit_message_text(
        "📋 SALIN AKTIVITAS\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {source.detail}\n📝 {source.object_work}\n"
        f"⏱ {source.total_minutes} menit\n\nKetik tanggal tujuan (DD/MM/YYYY).")
    return COPY_DATE


async def _finish_copy_target(message, context: ContextTypes.DEFAULT_TYPE, telegram_id: int):
    source: EMasterActivity = context.user_data["copy_source"]
    try:
        candidates = await asyncio.to_thread(get_client(telegram_id).search_kamus, source.detail)
    except AuthenticationRequired:
        await message.reply_text("🔐 Sesi habis. Login OTP lalu ulangi dari Riwayat.")
        return ConversationHandler.END
    except EMasterError as exc:
        await message.reply_text(f"❌ Kamus aktivitas belum dapat diperiksa: {exc}")
        return ConversationHandler.END
    item = next((candidate for candidate in candidates
                 if normalize_text(candidate.activity) == normalize_text(source.detail)), None)
    if not item:
        await message.reply_text(
            "⚠️ Aktivitas lama tidak ditemukan persis di kamus terbaru. Gunakan Tambah Aktivitas agar WPT tidak keliru.")
        return ConversationHandler.END
    context.user_data["item"] = item
    persist_draft(telegram_id, context, "VOLUME")
    await message.reply_text(
        f"✅ Data lama siap disalin\n{item.code} — {item.activity}\n"
        f"📦 {item.unit} · ⏱ {item.wpt} menit\n\n"
        f"🔢 Volume sebelumnya: {source.volume}\n"
        "Ketik volume baru atau '-' untuk memakai volume sebelumnya.")
    return COPY_VOLUME


@private
async def copy_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        date = parse_activity_date(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(f"⚠️ {exc}\nKetik tanggal DD/MM/YYYY.")
        return COPY_DATE
    context.user_data["date"] = date.strftime("%d/%m/%Y")
    persist_draft(update.effective_user.id, context, "TARGET")
    try:
        targets = await asyncio.to_thread(get_client(update.effective_user.id).list_work_targets,
                                          date.strftime("%m"))
    except AuthenticationRequired:
        await update.message.reply_text("🔐 Sesi habis. Draft tanggal tersimpan; login OTP lalu lanjutkan draft.")
        return ConversationHandler.END
    except EMasterError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return ConversationHandler.END
    source: EMasterActivity = context.user_data["copy_source"]
    matched = next((target for target in targets
                    if normalize_text(target.name) == normalize_text(source.target_name)), None)
    if matched:
        context.user_data["target"] = matched
        return await _finish_copy_target(update.effective_message, context, update.effective_user.id)
    context.user_data["copy_targets"] = targets
    buttons = [[InlineKeyboardButton(f"{index + 1}. {target.name[:48]}",
                                     callback_data=f"copytarget:{index}")]
               for index, target in enumerate(targets)]
    if not buttons:
        await update.message.reply_text("❌ Tugas jabatan untuk bulan tujuan tidak ditemukan.")
        return ConversationHandler.END
    await update.message.reply_text(
        "🎯 Pilih tugas jabatan tujuan. Tugas lama tidak ditemukan dengan nama yang sama.",
        reply_markup=InlineKeyboardMarkup(buttons))
    return COPY_TARGET


@private
async def copy_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.rsplit(":", 1)[1])
        target = context.user_data["copy_targets"][index]
    except (KeyError, IndexError, TypeError, ValueError):
        await query.edit_message_text("⚠️ Pilihan sudah kedaluwarsa. Ulangi dari Riwayat.")
        return ConversationHandler.END
    context.user_data["target"] = target
    await query.edit_message_text(f"✅ Tugas tujuan dipilih\n{target.name}")
    return await _finish_copy_target(query.message, context, update.effective_user.id)


@private
async def copy_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = context.user_data.get("copy_source")
    item = context.user_data.get("item")
    if not isinstance(source, EMasterActivity) or not isinstance(item, KamusItem):
        return ConversationHandler.END
    raw = update.message.text.strip()
    try:
        value = source.volume if raw == "-" else int(raw)
        if not 1 <= value <= 100 or item.wpt * value > 660:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Volume harus 1–100 dan WPT aktivitas maksimal 660 menit, atau ketik `-`.", parse_mode="Markdown")
        return COPY_VOLUME
    context.user_data["volume"] = value
    persist_draft(update.effective_user.id, context, "OBJECT")
    await update.message.reply_text(
        f"📝 Objek sebelumnya:\n{source.object_work}\n\n"
        "Ketik objek baru atau '-' untuk memakai objek sebelumnya.")
    return COPY_OBJECT


@private
async def copy_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = context.user_data.get("copy_source")
    if not isinstance(source, EMasterActivity):
        return ConversationHandler.END
    raw = update.message.text.strip()
    object_work = source.object_work if raw == "-" else raw
    if not 5 <= len(object_work) <= 1000:
        await update.message.reply_text("Objek kerja harus 5–1.000 karakter atau ketik `-`.", parse_mode="Markdown")
        return COPY_OBJECT
    context.user_data["object"] = object_work
    persist_draft(update.effective_user.id, context, "CONFIRM")
    return await prepare_add_confirmation(
        update.effective_message, context, update.effective_user.id,
        volume_state=COPY_VOLUME)


@private
async def delete_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.rsplit(":", 1)[1])
        item: EMasterActivity = context.user_data["history_items"][index]
    except (KeyError, IndexError, TypeError, ValueError):
        await query.edit_message_text("⚠️ Riwayat sudah kedaluwarsa. Buka /riwayat kembali.")
        return
    context.user_data["pending_delete"] = item
    text = ("⚠️ KONFIRMASI HAPUS\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Aktivitas ini akan dihapus langsung dari e‑Master dan tidak dapat dibatalkan.\n\n"
            f"📅 Tanggal: {item.date}\n"
            f"📌 Aktivitas: {item.detail}\n"
            f"📝 Objek kerja: {item.object_work}\n"
            f"⏱ WPT: {item.wpt} × {item.volume} = {item.total_minutes} menit")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Ya, hapus dari e‑Master",
                              callback_data=f"delete:confirm:{item.id_realisasi}")],
        [InlineKeyboardButton("↩️ Jangan hapus", callback_data="delete:cancel")],
    ])
    await query.edit_message_text(text, reply_markup=keyboard)


@private
async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        requested_id = query.data.rsplit(":", 1)[1]
        item: EMasterActivity = context.user_data["pending_delete"]
        if requested_id != item.id_realisasi:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        await query.edit_message_text("⚠️ Konfirmasi sudah kedaluwarsa. Buka /riwayat dan pilih aktivitas kembali.")
        return
    await query.edit_message_text("⏳ Menghapus aktivitas dan memeriksa ulang e‑Master…")
    try:
        client = get_client(update.effective_user.id)
        await asyncio.to_thread(client.delete_activity, item)
        storage.add_deleted(update.effective_user.id, item)
        storage.mark_daily_deleted(
            update.effective_user.id, activity_date_iso(item.date),
            item.detail, item.object_work)
    except AuthenticationRequired:
        context.user_data.pop("pending_delete", None)
        await query.edit_message_text("🔐 Sesi e‑Master habis. Jalankan /login, lalu ulangi dari /riwayat.")
        return
    except EMasterError as exc:
        context.user_data.pop("pending_delete", None)
        await query.edit_message_text(f"❌ Aktivitas belum terhapus: {exc}")
        return
    context.user_data.pop("history_items", None)
    context.user_data.pop("pending_delete", None)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Buka Riwayat", callback_data="menu:history"),
         InlineKeyboardButton("📊 Dashboard", callback_data="menu:progress")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")],
    ])
    await query.edit_message_text(
        f"✅ AKTIVITAS DIHAPUS DARI E‑MASTER\n\n{item.detail}\n📅 {item.date}\n⏱ {item.total_minutes} menit",
        reply_markup=keyboard)


@private
async def delete_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Penghapusan dibatalkan")
    context.user_data.pop("pending_delete", None)
    await query.edit_message_text(
        "✅ Aktivitas tidak dihapus.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Kembali ke Riwayat", callback_data="menu:history")]]))


@private
async def menu_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user = await sync_employee_profile(update.effective_user.id)
    await show_menu(update.callback_query.message, user, edit=True)


def _daily_history_text(rows, title: str) -> str:
    lines = [title, "━━━━━━━━━━━━━━━━━━━━"]
    if not rows:
        return "\n".join(lines + ["Belum ada aktivitas."])
    for index, row in enumerate(rows, start=1):
        photo = f"📷 {row[8]}/{row[7]} foto" if row[7] else "Tanpa foto"
        emaster = " · E-Master ✅" if row[5] == "sent" else ""
        lines.append(
            f"\n{index}. 📅 {row[1]} · {row[2]} WIB\n{row[3]}\n{photo}{emaster}")
    return "\n".join(lines)


@private
async def daily_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    today = now_jakarta().date().isoformat()
    rows = storage.list_daily_activities(update.effective_user.id, limit=30,
                                         activity_date=today)
    await update.effective_message.reply_text(
        _daily_history_text(rows, "📋 AKTIVITAS HARI INI"),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Tambah Aktivitas", callback_data="menu:add")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")],
        ]))


@private
async def daily_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    rows = storage.list_daily_activities(update.effective_user.id, limit=20)
    await update.effective_message.reply_text(
        _daily_history_text(rows, "🗓 RIWAYAT AKTIVITAS HARIAN"),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Hari Ini", callback_data="daily:today"),
             InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")],
        ]))


@private
async def evidence_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    rows = storage.recent_evidence(update.effective_user.id, 10)
    lines = ["☁️ BUKTI DUKUNG", "━━━━━━━━━━━━━━━━━━━━"]
    buttons = []
    if not rows:
        lines.append("Belum ada bukti foto.")
    for index, row in enumerate(rows, start=1):
        text = row[1] if len(row[1]) <= 70 else row[1][:67] + "…"
        state = "✅ Tersimpan" if row[4] == "uploaded" else "⚠️ Perlu dicoba lagi"
        lines.append(f"\n{index}. {row[0]} · {state}\n{text}")
        if row[3]:
            buttons.append([InlineKeyboardButton(f"📷 Buka Bukti {index}", url=row[3])])
        else:
            buttons.append([InlineKeyboardButton(
                f"🔄 Coba Lagi Bukti {index}", callback_data=f"evidence:retry:{row[5]}")])
    buttons.append([InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


@private
async def retry_evidence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        evidence_id = int(query.data.rsplit(":", 1)[1])
    except ValueError:
        await query.edit_message_text("⚠️ Bukti tidak valid.")
        return
    row = storage.get_evidence(evidence_id)
    if not row or row[9] != update.effective_user.id:
        await query.edit_message_text("⚠️ Bukti tidak ditemukan.")
        return
    if row[8] == "uploaded":
        await query.edit_message_text(
            "✅ Bukti sudah tersimpan.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "☁️ Lihat Bukti Dukung", callback_data="daily:evidence")]]))
        return
    context.user_data["photos"] = [{
        "file_id": row[2], "unique_id": row[3],
        "mime_type": row[4] or "image/jpeg",
        "extension": Path(row[5] or "bukti.jpg").suffix or ".jpg",
        "file_name": row[5] or None,
    }]
    user = storage.get_user(update.effective_user.id)
    uploaded, _, _ = await upload_activity_photos(
        context, user, row[1], row[10], row[11])
    context.user_data.pop("photos", None)
    if uploaded:
        await query.edit_message_text(
            "✅ Bukti foto berhasil diproses.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "☁️ Lihat Bukti Dukung", callback_data="daily:evidence")]]))
    else:
        await query.edit_message_text(
            "⚠️ Bukti foto masih belum dapat diproses. Silakan coba beberapa saat lagi.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔄 Coba Lagi", callback_data=f"evidence:retry:{evidence_id}")]]))


@private
async def profile_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    user = storage.get_user(update.effective_user.id)
    kind = employee_type(user)
    identifier = "NIP" if kind == "asn" else "ID Pegawai"
    await update.effective_message.reply_text(
        "👤 PROFIL PEGAWAI\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Nama: {user[3] or '-'}\n{identifier}: {user[1]}\n"
        f"Jenis: {'ASN' if kind == 'asn' else 'Non-ASN'}\n"
        f"Jabatan: {user[6] or '-'}\nUnit Kerja: {employee_unit(user)}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "🏠 Menu Utama", callback_data="menu:home")]]))


@private
async def help_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    kind = employee_type(storage.get_user(update.effective_user.id))
    detail = ("Pilih kamus dan tugas jabatan, lalu kirim uraian sebagai teks atau "
              "foto dengan caption. Uraian aktivitas akan diteruskan ke E-Master."
              if kind == "asn" else
              "Kirim uraian sebagai teks atau foto dengan caption. Aktivitas Non-ASN "
              "dicatat tanpa login E-Master.")
    await update.effective_message.reply_text(
        "❓ BANTUAN\n━━━━━━━━━━━━━━━━━━━━\n"
        f"• Tambah Aktivitas: {detail}\n"
        "• Secara otomatis Laporan WFH dapat dibuat pada Jumat sampai pukul 23.59 WIB; "
        "admin dapat membuka atau menutup generate secara manual.\n"
        "• Aktivitas laporan harus memiliki tanggal Jumat dan benar-benar dikirim pada Jumat tersebut.\n"
        "• Jika foto gagal diproses, gunakan tombol Coba Lagi Foto.\n"
        "• Uraian hanya dirapikan secara dasar tanpa layanan AI.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "🏠 Menu Utama", callback_data="menu:home")]]))


@private
async def signature_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = storage.get_user(update.effective_user.id)
    available = len(user) > 11 and bool(user[11])
    status = "✅ Sudah tersimpan" if available else "⚠️ Belum diunggah"
    await query.message.reply_text(
        "✍️ TANDA TANGAN LAPORAN WFH\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Status: {status}\n\n"
        "Gunakan PNG transparan agar hasil terlihat natural. Tanda tangan akan "
        "dipangkas otomatis, dibuat proporsional, dan hanya dipakai pada laporan Anda.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔄 Ganti PNG" if available else "⬆️ Unggah PNG",
                callback_data="signature:upload")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")],
        ]))


@private
async def signature_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "Kirim tanda tangan sebagai file PNG (bukan sebagai foto).\n\n"
        "Batas ukuran 5 MB. Area transparan berlebih akan dipangkas otomatis. "
        "Ketik /batal untuk membatalkan.")
    return SIGNATURE_UPLOAD


@private
async def signature_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.effective_message.document
    if not document:
        await update.effective_message.reply_text(
            "❌ Kirim tanda tangan melalui ikon lampiran sebagai file PNG, bukan foto.")
        return SIGNATURE_UPLOAD
    file_name = document.file_name or "tanda_tangan.png"
    if not file_name.lower().endswith(".png"):
        await update.effective_message.reply_text("❌ Format harus PNG.")
        return SIGNATURE_UPLOAD
    if document.file_size and document.file_size > 5 * 1024 * 1024:
        await update.effective_message.reply_text("❌ Ukuran PNG maksimal 5 MB.")
        return SIGNATURE_UPLOAD

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        content = bytes(await telegram_file.download_as_bytearray())
        with Image.open(io.BytesIO(content)) as image:
            if image.format != "PNG":
                raise ValueError("File bukan PNG")
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            if image.width < 50 or image.height < 50:
                raise ValueError("Resolusi PNG terlalu kecil")
            if image.width > 6000 or image.height > 6000:
                raise ValueError("Resolusi PNG terlalu besar")
    except (UnidentifiedImageError, OSError, ValueError):
        await update.effective_message.reply_text(
            "❌ File PNG tidak valid. Gunakan gambar tanda tangan yang dapat dibuka normal.")
        return SIGNATURE_UPLOAD

    user = storage.get_user(update.effective_user.id)
    try:
        with tempfile.TemporaryDirectory(prefix="signature_") as temp_dir:
            local_signature = Path(temp_dir) / "Tanda_Tangan.png"
            local_signature.write_bytes(content)
            employee_folder_id = await asyncio.to_thread(
                drive_storage.employee_folder, user[3] or str(user[0]), user[10])
            if not user[10]:
                storage.set_drive_folder_id(user[0], employee_folder_id)
            destination_id = await asyncio.to_thread(
                drive_storage.signature_folder, employee_folder_id)
            existing_file_id = user[11] if len(user) > 11 else None
            drive_file_id, drive_url = await asyncio.to_thread(
                drive_storage.upload_or_update_file,
                local_path=local_signature,
                file_name="Tanda_Tangan.png",
                mime_type="image/png",
                parent_id=destination_id,
                existing_file_id=existing_file_id,
            )
        storage.set_signature(
            user[0], drive_file_id=drive_file_id,
            drive_url=drive_url, file_name="Tanda_Tangan.png")
    except DriveStorageError as exc:
        await update.effective_message.reply_text(
            f"❌ Tanda tangan belum dapat disimpan: {exc}")
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "✅ TANDA TANGAN BERHASIL DISIMPAN\n\n"
        "Tanda tangan ini akan dimasukkan otomatis ke laporan WFH Word dan PDF Anda.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "🏠 Menu Utama", callback_data="menu:home")]]))
    return ConversationHandler.END


def _generate_report_sync(user, moment: datetime, report_date):
    rows = storage.list_wfh_activities(user[0], report_date.isoformat())
    if not rows:
        return None
    with tempfile.TemporaryDirectory(prefix="wfh_report_") as temp_dir:
        temp_path = Path(temp_dir)
        report_activities = []
        for activity_row, evidence_rows in rows:
            evidence_files = []
            for evidence_row in evidence_rows:
                local_path = None
                if evidence_row[2] and (evidence_row[4] or "").startswith("image/"):
                    suffix = Path(evidence_row[1] or "bukti.jpg").suffix or ".jpg"
                    candidate = temp_path / f"evidence_{evidence_row[0]}{suffix}"
                    try:
                        local_path = drive_storage.download_file(evidence_row[2], candidate)
                    except DriveStorageError:
                        logging.warning("Thumbnail bukti %s tidak dapat diunduh; hyperlink tetap dipakai",
                                        evidence_row[0])
                if evidence_row[3]:
                    evidence_files.append(EvidenceFile(
                        url=evidence_row[3], local_path=local_path,
                        file_name=evidence_row[1] or ""))
            report_activities.append(ReportActivity(
                activity_time=activity_row[2], text=activity_row[3],
                evidence=evidence_files))
        file_name = report_file_name(report_date)
        pdf_file_name = report_pdf_file_name(report_date)
        local_report = temp_path / file_name
        signature_path = None
        if len(user) > 11 and user[11]:
            signature_path = drive_storage.download_file(
                user[11], temp_path / "signature.png")
        build_wfh_report(
            template_path=REPORT_TEMPLATE, output_path=local_report,
            employee_name=user[3] or str(user[0]), employee_identifier=user[1],
            employee_type=employee_type(user),
            position=user[6] or ("Pegawai Non-ASN" if employee_type(user) == "non_asn" else "-"),
            unit_name=employee_unit(user), report_date=report_date,
            activities=report_activities, signature_path=signature_path)
        local_pdf = convert_docx_to_pdf(local_report, temp_path)
        if local_pdf.name != pdf_file_name:
            expected_pdf = temp_path / pdf_file_name
            local_pdf.replace(expected_pdf)
            local_pdf = expected_pdf
        employee_folder_id = drive_storage.employee_folder(
            user[3] or str(user[0]), user[10])
        if not user[10]:
            storage.set_drive_folder_id(user[0], employee_folder_id)
        destination_id = drive_storage.report_folder(
            employee_folder_id, report_date.year, report_date.month)
        existing = storage.get_report(user[0], report_date.isoformat())
        existing_file_id = existing[2] if existing and existing[5] == "uploaded" else None
        file_id, file_url = drive_storage.upload_or_update_file(
            local_path=local_report, file_name=file_name,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            parent_id=destination_id, existing_file_id=existing_file_id)
        existing_pdf_file_id = existing[9] if existing and existing[5] == "uploaded" else None
        pdf_file_id, pdf_file_url = drive_storage.upload_or_update_file(
            local_path=local_pdf, file_name=pdf_file_name,
            mime_type="application/pdf", parent_id=destination_id,
            existing_file_id=existing_pdf_file_id)
        storage.save_report(
            telegram_id=user[0], report_date=report_date.isoformat(),
            file_name=file_name, drive_file_id=file_id, drive_url=file_url,
            pdf_file_name=pdf_file_name, pdf_drive_file_id=pdf_file_id,
            pdf_drive_url=pdf_file_url,
            activity_count=len(report_activities),
            generated_at_local=moment.isoformat(timespec="seconds"))
        return GeneratedWfhReport(
            word_name=file_name,
            word_url=file_url,
            word_content=local_report.read_bytes(),
            pdf_name=pdf_file_name,
            pdf_url=pdf_file_url,
            pdf_content=local_pdf.read_bytes(),
            activity_count=len(report_activities),
            signature_included=signature_path is not None,
        )


@private
async def generate_wfh_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    moment = now_jakarta()
    user = storage.get_user(update.effective_user.id)
    access_mode = storage.wfh_report_access_mode()
    if not report_generation_allowed(moment, access_mode):
        latest = storage.latest_report(user[0])
        buttons = [[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")]]
        if latest and latest[2]:
            drive_buttons = [InlineKeyboardButton("📝 Word Terakhir", url=latest[2])]
            if len(latest) > 7 and latest[7]:
                drive_buttons.append(InlineKeyboardButton("📕 PDF Terakhir", url=latest[7]))
            buttons.insert(0, drive_buttons)
        reason = ("Generate Laporan WFH sedang ditutup secara manual oleh admin."
                  if access_mode == "closed" else
                  "Laporan hanya dapat dibuat setiap hari Jumat sampai pukul 23.59 WIB.")
        await update.effective_message.reply_text(
            f"⛔ PERIODE LAPORAN WFH DITUTUP\n\n{reason}",
            reply_markup=InlineKeyboardMarkup(buttons))
        return
    report_date = report_date_for_access(moment, access_mode)
    report_lock = report_locks.setdefault(user[0], asyncio.Lock())
    if report_lock.locked():
        await update.effective_message.reply_text(
            "⏳ Laporan Anda sedang diproses. Mohon tunggu sampai tombol buka laporan muncul.")
        return
    status = await update.effective_message.reply_text(
        "⏳ Menyiapkan Laporan WFH. Mohon tunggu dan jangan menekan tombol berulang kali…")
    async with report_lock:
        try:
            result = await asyncio.to_thread(
                _generate_report_sync, user, moment, report_date)
            if not result:
                await status.edit_text(
                    "ℹ️ Belum ada aktivitas yang memenuhi syarat untuk Laporan WFH.\n\n"
                    f"Aktivitas harus bertanggal {format_indonesian_date(report_date)} "
                    "dan dikirim pada tanggal yang sama.")
                return
            await status.edit_text(
                "✅ LAPORAN WFH BERHASIL DIBUAT\n\n"
                f"📅 Periode: {format_indonesian_date(report_date)}\n"
                f"📝 Jumlah aktivitas: {result.activity_count}\n"
                f"✍️ Tanda tangan: {'sudah dimasukkan' if result.signature_included else 'belum tersedia'}\n"
                "☁️ Word dan PDF sudah tersimpan di Google Drive.\n"
                "📥 Kedua file dikirim di bawah pesan ini.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Buka Word", url=result.word_url),
                     InlineKeyboardButton("📕 Buka PDF", url=result.pdf_url)],
                    [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu:home")],
                ]))
            try:
                await update.effective_message.reply_document(
                    document=InputFile(io.BytesIO(result.word_content),
                                       filename=result.word_name),
                    caption="📝 Laporan WFH versi Word")
                await update.effective_message.reply_document(
                    document=InputFile(io.BytesIO(result.pdf_content),
                                       filename=result.pdf_name),
                    caption="📕 Laporan WFH versi PDF")
            except Exception:
                logging.exception(
                    "Laporan sudah tersimpan di Drive tetapi pengiriman Telegram gagal untuk ID %s",
                    user[0])
                await update.effective_message.reply_text(
                    "⚠️ File sudah aman di Google Drive, tetapi pengiriman langsung ke Telegram "
                    "belum berhasil. Gunakan tombol Word atau PDF di atas.")
        except Exception as exc:
            file_name = report_file_name(report_date)
            storage.save_report_error(user[0], report_date.isoformat(), file_name, str(exc))
            logging.exception("Pembuatan laporan WFH gagal untuk Telegram ID %s", user[0])
            message = str(exc) if isinstance(exc, DriveStorageError) else "Laporan belum dapat dibuat."
            await status.edit_text(
                f"❌ {message}\nData aktivitas tetap aman. Silakan coba kembali saat akses laporan dibuka.")


@private
async def refresh_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Memperbarui profil…")
    try:
        user = await sync_employee_profile(update.effective_user.id, strict=True)
    except StaffDirectoryError as exc:
        await query.message.reply_text(f"❌ Profil belum dapat diperbarui: {exc}")
        return
    await show_menu(query.message, user, edit=True)


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
        "👥 *TAMBAH PEGAWAI*\n\nPilih jenis pegawai:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🟦 ASN", callback_data="admin:type:asn"),
            InlineKeyboardButton("🟩 Non-ASN", callback_data="admin:type:non_asn"),
        ]]))
    return ADMIN_TYPE


async def employee_type_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind = query.data.rsplit(":", 1)[1]
    if kind not in {"asn", "non_asn"}:
        await query.edit_message_text("Jenis pegawai tidak valid.")
        return ConversationHandler.END
    context.user_data["new_employee_type"] = kind
    await query.edit_message_text(
        "Masukkan Telegram ID pegawai.\nPegawai dapat melihat ID melalui /start.")
    return ADMIN_TGID


async def employee_tgid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        telegram_id = int(update.message.text.strip())
        if telegram_id <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Telegram ID harus berupa angka.")
        return ADMIN_TGID
    existing = storage.get_user(telegram_id)
    if existing and existing[5]:
        await update.message.reply_text("Akun admin tidak dapat didaftarkan ulang dari menu pegawai.")
        return ADMIN_TGID
    context.user_data["new_telegram_id"] = telegram_id
    label = "NIP" if context.user_data.get("new_employee_type") == "asn" else "ID Pegawai/NIK"
    await update.message.reply_text(f"Masukkan {label}:")
    return ADMIN_NIP


async def employee_nip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nip = update.message.text.strip()
    if context.user_data.get("new_employee_type") == "asn":
        if not nip.isdigit() or not 10 <= len(nip) <= 25:
            await update.message.reply_text("NIP tidak valid. Masukkan angka NIP lengkap.")
            return ADMIN_NIP
    elif not 3 <= len(nip) <= 50 or any(ord(character) < 32 for character in nip):
        await update.message.reply_text("ID Pegawai/NIK harus berisi 3–50 karakter.")
        return ADMIN_NIP
    context.user_data["new_nip"] = nip
    try:
        await update.message.delete()
    except Exception:
        logging.warning("Pesan NIP tidak dapat dihapus otomatis; tidak ada NIP yang dicatat di log")
    await context.bot.send_message(update.effective_user.id, "Masukkan nama pegawai:")
    return ADMIN_NAME


async def employee_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name or len(name) > 100:
        await update.message.reply_text("Nama harus 1–100 karakter.")
        return ADMIN_NAME
    telegram_id = context.user_data["new_telegram_id"]
    kind = context.user_data.get("new_employee_type", "asn")
    storage.invite_user(telegram_id, context.user_data["new_nip"], name,
                        employee_type=kind,
                        unit_name=os.getenv(
                            "DEFAULT_UNIT_KERJA",
                            "Bidang Pemasaran dan Kelembagaan Parekraf"))
    clear_cached_user(telegram_id)
    next_step = ("Minta pegawai membuka bot, tekan /start, lalu /aktifkan."
                 if kind == "asn" else
                 "Akun Non-ASN langsung aktif. Minta pegawai membuka bot lalu tekan /start.")
    await update.message.reply_text(
        f"✅ PEGAWAI DITAMBAHKAN\n\nNama: {name}\n"
        f"Jenis: {'ASN' if kind == 'asn' else 'Non-ASN'}\nTelegram ID: {telegram_id}\n\n"
        f"{next_step}")
    try:
        instruction = ("Jalankan /aktifkan untuk memasukkan password pribadi."
                       if kind == "asn" else "Akun Anda sudah aktif. Jalankan /start.")
        await context.bot.send_message(
            telegram_id,
            f"👋 Halo {name}, Anda telah didaftarkan ke E-Master Jatim sebagai "
            f"{'ASN' if kind == 'asn' else 'Non-ASN'}.\n{instruction}")
    except Exception:
        logging.info("Undangan tidak dapat dikirim langsung; pegawai tetap dapat membuka bot sendiri")
    context.user_data.clear()
    return ConversationHandler.END


async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = storage.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("⛔ Telegram ID Anda belum didaftarkan admin.")
        return ConversationHandler.END
    if user[4] == "active":
        instruction = "Gunakan /login." if employee_type(user) == "asn" else "Gunakan /start."
        await update.message.reply_text(f"✅ Akun sudah aktif. {instruction}")
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
    password_message_deleted = True
    try:
        await update.message.delete()
    except Exception:
        password_message_deleted = False
        logging.warning("Pesan password tidak dapat dihapus otomatis; tidak ada password yang dicatat di log")
    if len(password) < 4 or len(password) > 256:
        await context.bot.send_message(update.effective_user.id, "Password harus 4–256 karakter. Jalankan /aktifkan kembali.")
        return ConversationHandler.END
    encrypted = fernet.encrypt(password.encode()).decode()
    storage.activate_user(update.effective_user.id, encrypted)
    clear_cached_user(update.effective_user.id)
    notice = ("\n\n⚠️ Telegram tidak mengizinkan penghapusan otomatis. "
              "Hapus pesan password Anda secara manual.") if not password_message_deleted else ""
    await context.bot.send_message(update.effective_user.id,
        "✅ *AKUN AKTIF*\nPassword telah dienkripsi. Sekarang jalankan /login dan masukkan OTP Anda." + notice,
        parse_mode="Markdown")
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
    maintenance = storage.maintenance_active()
    text.append(f"\n🔧 Maintenance: {'AKTIF' if maintenance else 'NONAKTIF'}")
    buttons.append([InlineKeyboardButton(
        "✅ Selesaikan Maintenance" if maintenance else "🔧 Mulai Maintenance",
        callback_data="admin:maintenance:off" if maintenance else "admin:maintenance:on")])
    report_mode = storage.wfh_report_access_mode()
    report_labels = {
        "auto": "OTOMATIS — Jumat 00.00–23.59 WIB",
        "open": "DIBUKA MANUAL",
        "closed": "DITUTUP MANUAL",
    }
    text.append(f"\n📄 Generate Laporan WFH: {report_labels[report_mode]}")
    buttons.extend([
        [InlineKeyboardButton(
            "✅ Buka Manual" if report_mode != "open" else "✅ Dibuka Manual",
            callback_data="admin:report:open"),
         InlineKeyboardButton(
            "⛔ Tutup Manual" if report_mode != "closed" else "⛔ Ditutup Manual",
            callback_data="admin:report:closed")],
        [InlineKeyboardButton(
            "🗓 Kembali ke Jadwal Jumat",
            callback_data="admin:report:auto")],
    ])
    for tid, nip, name, status, is_admin, kind in rows:
        icon = "👑" if is_admin else ("✅" if status == "active" else "⏳" if status == "invited" else "⛔")
        type_label = "ASN" if kind == "asn" else "Non-ASN"
        text.append(f"\n{icon} {name or 'Tanpa nama'}\nID: {tid} · {type_label} · Status: {status}")
        if not is_admin and status != "disabled":
            buttons.append([InlineKeyboardButton(f"⛔ Nonaktifkan {name or tid}", callback_data=f"admin:disable:{tid}")])
    await update.effective_message.reply_text("\n".join(text), reply_markup=InlineKeyboardMarkup(buttons))


async def disable_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if not admin_only(update):
        return
    telegram_id = int(update.callback_query.data.split(":")[2])
    storage.disable_user(telegram_id)
    clear_cached_user(telegram_id)
    await update.callback_query.edit_message_text("✅ Pegawai dinonaktifkan. Data pegawai lain tidak berubah.")


async def broadcast_notification(application: Application, text: str):
    delivered = 0
    for telegram_id in storage.list_notification_user_ids():
        try:
            await application.bot.send_message(telegram_id, text)
            delivered += 1
        except Exception:
            logging.info("Notifikasi sistem tidak terkirim ke Telegram ID %s", telegram_id)
        await asyncio.sleep(0.04)
    return delivered


async def report_access_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not admin_only(update):
        await query.edit_message_text("⛔ Khusus admin.")
        return
    mode = query.data.rsplit(":", 1)[1]
    storage.set_wfh_report_access_mode(mode)
    moment = now_jakarta()
    if mode == "open":
        report_date = report_date_for_access(moment, mode)
        notice = (
            "✅ GENERATE LAPORAN WFH DIBUKA\n\n"
            f"Admin membuka generate manual untuk periode "
            f"{format_indonesian_date(report_date)}. Gunakan /laporan untuk membuat laporan.")
        confirmation = f"✅ Generate manual dibuka untuk periode {format_indonesian_date(report_date)}."
    elif mode == "closed":
        notice = (
            "⛔ GENERATE LAPORAN WFH DITUTUP\n\n"
            "Admin menutup sementara pembuatan laporan. Laporan terakhir tetap dapat dibuka.")
        confirmation = "⛔ Generate Laporan WFH ditutup secara manual."
    else:
        notice = (
            "🗓 JADWAL LAPORAN WFH OTOMATIS\n\n"
            "Generate laporan kembali mengikuti jadwal Jumat pukul 00.00–23.59 WIB.")
        confirmation = "🗓 Generate laporan kembali mengikuti jadwal Jumat."
    delivered = await broadcast_notification(context.application, notice)
    await query.edit_message_text(
        f"{confirmation}\nNotifikasi dikirim kepada {delivered} pengguna aktif.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "👥 Kembali", callback_data="menu:users")]]))


async def maintenance_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not admin_only(update):
        await query.edit_message_text("⛔ Khusus admin.")
        return
    action = query.data.rsplit(":", 1)[1]
    if action == "on":
        if storage.maintenance_active():
            await query.edit_message_text("ℹ️ Mode maintenance sudah aktif.")
            return
        storage.set_setting("maintenance_active", "1")
        storage.set_setting("maintenance_started_at", now_jakarta().isoformat(timespec="seconds"))
        delivered = await broadcast_notification(
            context.application,
            "🔧 BOT SEDANG DALAM PERBAIKAN\n\n"
            "Saat ini E-Master Jatim sedang menjalani pembaruan sistem. "
            "Beberapa layanan untuk sementara tidak dapat digunakan. "
            "Mohon menunggu sampai proses selesai.")
        await query.edit_message_text(
            f"🔧 Mode maintenance aktif. Notifikasi dikirim kepada {delivered} pengguna aktif.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "👥 Kembali", callback_data="menu:users")]]))
        return
    if not storage.maintenance_active():
        await query.edit_message_text("ℹ️ Mode maintenance sudah nonaktif.")
        return
    try:
        storage.db.execute("SELECT 1").fetchone()
        await asyncio.to_thread(drive_storage.healthcheck)
    except Exception as exc:
        message = str(exc) if isinstance(exc, DriveStorageError) else "Pemeriksaan sistem gagal."
        await query.edit_message_text(
            f"⚠️ Maintenance belum dapat diselesaikan. {message}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔄 Periksa Lagi", callback_data="admin:maintenance:off")]]))
        return
    storage.set_setting("maintenance_active", "0")
    storage.set_setting("maintenance_finished_at", now_jakarta().isoformat(timespec="seconds"))
    delivered = await broadcast_notification(
        context.application,
        "✅ PERBAIKAN TELAH SELESAI\n\n"
        "E-Master Jatim sudah dapat digunakan kembali. "
        "Silakan buka /start untuk melanjutkan aktivitas.")
    await query.edit_message_text(
        f"✅ Mode maintenance selesai. Notifikasi dikirim kepada {delivered} pengguna aktif.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "🏠 Menu Utama", callback_data="menu:home")]]))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.exception("Unhandled Telegram bot error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Terjadi gangguan saat memproses tombol. Data belum diubah. "
                "Buka /start lalu coba kembali.")
        except Exception:
            logging.debug("Pesan kesalahan tidak dapat dikirim ke pengguna")


@private
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage.delete_draft(update.effective_user.id)
    context.user_data.clear()
    await update.message.reply_text("Pengisian dibatalkan.")
    return ConversationHandler.END


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Selalu jawab callback lama agar Telegram tidak terlihat macet."""
    query = update.callback_query
    if not query:
        return
    await query.answer("Tombol sudah kedaluwarsa. Buka /start.", show_alert=True)


async def configure_bot_commands(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Buka menu utama"),
            BotCommand("tambah", "Tambah aktivitas harian"),
            BotCommand("dashboard", "Lihat progres WPT terbaru"),
            BotCommand("riwayat", "Edit, salin, atau hapus aktivitas"),
            BotCommand("laporan", "Buat Laporan WFH hari Jumat"),
            BotCommand("login", "Login e-Master dengan OTP baru"),
            BotCommand("batal", "Batalkan proses yang berjalan"),
            BotCommand("aktifkan", "Aktifkan akun pegawai baru"),
        ])
    except Exception:
        logging.warning("Daftar perintah Telegram belum dapat diperbarui")
    if (storage.maintenance_active()
            and os.getenv("AUTO_END_MAINTENANCE_ON_START", "true").lower() == "true"):
        try:
            storage.db.execute("SELECT 1").fetchone()
            await asyncio.to_thread(drive_storage.healthcheck)
            storage.set_setting("maintenance_active", "0")
            storage.set_setting("maintenance_finished_at", now_jakarta().isoformat(timespec="seconds"))
            await broadcast_notification(
                application,
                "✅ PERBAIKAN TELAH SELESAI\n\n"
                "E-Master Jatim sudah dapat digunakan kembali. "
                "Silakan buka /start untuk melanjutkan aktivitas.")
        except Exception:
            logging.exception("Maintenance tetap aktif karena pemeriksaan awal belum berhasil")


def main():
    app = (Application.builder().token(os.environ["BOT_TOKEN"])
           .post_init(configure_bot_commands).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("tambahpegawai", add_employee),
                      CallbackQueryHandler(add_employee, pattern=r"^admin:add$")],
        states={ADMIN_TYPE: [CallbackQueryHandler(employee_type_choice,
                                                  pattern=r"^admin:type:(?:asn|non_asn)$")],
                ADMIN_TGID: [MessageHandler(filters.TEXT & ~filters.COMMAND, employee_tgid)],
                ADMIN_NIP: [MessageHandler(filters.TEXT & ~filters.COMMAND, employee_nip)],
                ADMIN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, employee_name)]},
        fallbacks=[CommandHandler("batal", cancel)]))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("aktifkan", activate)],
        states={ACTIVATE_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, activate_password)]},
        fallbacks=[CommandHandler("batal", cancel)]))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(signature_begin, pattern=r"^signature:upload$")],
        states={SIGNATURE_UPLOAD: [MessageHandler(
            filters.ALL & ~filters.COMMAND, signature_receive)]},
        fallbacks=[CommandHandler("batal", cancel)], allow_reentry=True))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("login", login),
                      CallbackQueryHandler(login, pattern=r"^menu:login$"),
                      CommandHandler("tambah", add),
                      CallbackQueryHandler(add, pattern=r"^menu:(?:add|newadd)$"),
                      CallbackQueryHandler(resume_draft, pattern=r"^menu:resume$")],
        states={
        OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp)],
        DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date),
               CallbackQueryHandler(quick_date, pattern=r"^date:")],
        TARGET: [CallbackQueryHandler(pick_target, pattern=r"^target:\d+$")],
        SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, search),
                 CallbackQueryHandler(pick_favorite, pattern=r"^favorite:pick:\d+$")],
        PICK: [CallbackQueryHandler(pick, pattern=r"^pick:\d+$"),
               CallbackQueryHandler(navigate_kamus, pattern=r"^kamus:(?:page:\d+|search)$")],
        VOLUME: [MessageHandler(filters.TEXT & ~filters.COMMAND, volume)],
        OBJECT: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, object_work)],
        CONFIRM: [CallbackQueryHandler(confirm,
                                       pattern=r"^(?:send|send_duplicate|cancel|add_photo)$")],
        DAILY_ACTIVITY: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
                                        daily_activity_input)],
        DAILY_CONFIRM: [CallbackQueryHandler(
            daily_activity_confirm, pattern=r"^daily:(?:save|edit|cancel|addphoto)$")],
        ADDITIONAL_PHOTO: [MessageHandler((filters.PHOTO | filters.TEXT) & ~filters.COMMAND,
                                          additional_photo_input)],
    }, fallbacks=[CommandHandler("batal", cancel)], allow_reentry=True))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_begin, pattern=r"^edit:pick:\d+$")],
        states={
            EDIT_ACTIVITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_search_activity),
                            CallbackQueryHandler(edit_keep_activity,
                                                 pattern=r"^edit:(?:keepactivity|cancel)$")],
            EDIT_PICK: [CallbackQueryHandler(edit_pick_activity, pattern=r"^edititem:pick:\d+$"),
                        CallbackQueryHandler(edit_navigate_kamus,
                                             pattern=r"^editkamus:(?:page:\d+|search)$")],
            EDIT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_date)],
            EDIT_VOLUME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_volume)],
            EDIT_OBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_object)],
            EDIT_CONFIRM: [CallbackQueryHandler(
                edit_confirm, pattern=r"^edit:(?:save:\d+|cancel)$")],
        }, fallbacks=[CommandHandler("batal", cancel)], allow_reentry=True))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(copy_begin, pattern=r"^copy:pick:\d+$")],
        states={
            COPY_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_date)],
            COPY_TARGET: [CallbackQueryHandler(copy_target, pattern=r"^copytarget:\d+$")],
            COPY_VOLUME: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_volume)],
            COPY_OBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, copy_object)],
            CONFIRM: [CallbackQueryHandler(confirm,
                                           pattern=r"^(?:send|send_duplicate|cancel|add_photo)$")],
            ADDITIONAL_PHOTO: [MessageHandler((filters.PHOTO | filters.TEXT) & ~filters.COMMAND,
                                              additional_photo_input)],
        }, fallbacks=[CommandHandler("batal", cancel)], allow_reentry=True))
    app.add_handler(CommandHandler("progres", progress))
    app.add_handler(CommandHandler("dashboard", progress))
    app.add_handler(CommandHandler("riwayat", history))
    app.add_handler(CommandHandler("laporan", generate_wfh_report))
    app.add_handler(CommandHandler("batal", cancel))
    app.add_handler(CallbackQueryHandler(progress, pattern=r"^menu:progress$"))
    app.add_handler(CallbackQueryHandler(history, pattern=r"^menu:history$"))
    app.add_handler(CallbackQueryHandler(history_page, pattern=r"^history:page:\d+$"))
    app.add_handler(CallbackQueryHandler(delete_preview, pattern=r"^delete:pick:\d+$"))
    app.add_handler(CallbackQueryHandler(delete_confirm, pattern=r"^delete:confirm:\d+$"))
    app.add_handler(CallbackQueryHandler(delete_cancel, pattern=r"^delete:cancel$"))
    app.add_handler(CallbackQueryHandler(menu_home, pattern=r"^menu:home$"))
    app.add_handler(CallbackQueryHandler(refresh_profile, pattern=r"^menu:profile$"))
    app.add_handler(CallbackQueryHandler(profile_view, pattern=r"^menu:profileview$"))
    app.add_handler(CallbackQueryHandler(help_view, pattern=r"^menu:help$"))
    app.add_handler(CallbackQueryHandler(signature_menu, pattern=r"^signature:menu$"))
    app.add_handler(CallbackQueryHandler(daily_today, pattern=r"^daily:today$"))
    app.add_handler(CallbackQueryHandler(daily_history, pattern=r"^daily:history$"))
    app.add_handler(CallbackQueryHandler(evidence_list, pattern=r"^daily:evidence$"))
    app.add_handler(CallbackQueryHandler(retry_evidence, pattern=r"^evidence:retry:\d+$"))
    app.add_handler(CallbackQueryHandler(generate_wfh_report, pattern=r"^report:generate$"))
    app.add_handler(CallbackQueryHandler(favorites_menu, pattern=r"^menu:favorites$"))
    app.add_handler(CallbackQueryHandler(add_favorite, pattern=r"^favorite:add:\d+$"))
    app.add_handler(CallbackQueryHandler(remove_favorite, pattern=r"^favorite:remove:\d+$"))
    app.add_handler(CallbackQueryHandler(users_menu, pattern=r"^menu:users$"))
    app.add_handler(CallbackQueryHandler(disable_employee, pattern=r"^admin:disable:\d+$"))
    app.add_handler(CallbackQueryHandler(
        maintenance_toggle, pattern=r"^admin:maintenance:(?:on|off)$"))
    app.add_handler(CallbackQueryHandler(
        report_access_toggle, pattern=r"^admin:report:(?:auto|open|closed)$"))
    app.add_handler(CallbackQueryHandler(stale_button))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
