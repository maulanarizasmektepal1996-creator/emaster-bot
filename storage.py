import sqlite3
import threading
from pathlib import Path


class Storage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("""CREATE TABLE IF NOT EXISTS activities (
          id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL DEFAULT 0,
          activity_date TEXT NOT NULL,
          code TEXT NOT NULL, activity TEXT NOT NULL, unit TEXT NOT NULL,
          wpt INTEGER NOT NULL, volume INTEGER NOT NULL, object_work TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          sent_at TEXT
        )""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(activities)")}
        if "telegram_id" not in columns:
            self.db.execute("ALTER TABLE activities ADD COLUMN telegram_id INTEGER NOT NULL DEFAULT 0")
        self.db.execute("""CREATE TABLE IF NOT EXISTS users (
          telegram_id INTEGER PRIMARY KEY, nip TEXT NOT NULL,
          password_enc TEXT, full_name TEXT, status TEXT NOT NULL DEFAULT 'invited',
          is_admin INTEGER NOT NULL DEFAULT 0, position TEXT,
          profile_updated_at TEXT, signature_drive_file_id TEXT,
          signature_drive_url TEXT, signature_file_name TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        user_columns = {row[1] for row in self.db.execute("PRAGMA table_info(users)")}
        if "position" not in user_columns:
            self.db.execute("ALTER TABLE users ADD COLUMN position TEXT")
        if "profile_updated_at" not in user_columns:
            self.db.execute("ALTER TABLE users ADD COLUMN profile_updated_at TEXT")
        if "employee_type" not in user_columns:
            self.db.execute("ALTER TABLE users ADD COLUMN employee_type TEXT NOT NULL DEFAULT 'asn'")
        if "unit_name" not in user_columns:
            self.db.execute("ALTER TABLE users ADD COLUMN unit_name TEXT NOT NULL DEFAULT 'Pemasaran'")
        if "drive_folder_id" not in user_columns:
            self.db.execute("ALTER TABLE users ADD COLUMN drive_folder_id TEXT")
        if "signature_drive_file_id" not in user_columns:
            self.db.execute("ALTER TABLE users ADD COLUMN signature_drive_file_id TEXT")
        if "signature_drive_url" not in user_columns:
            self.db.execute("ALTER TABLE users ADD COLUMN signature_drive_url TEXT")
        if "signature_file_name" not in user_columns:
            self.db.execute("ALTER TABLE users ADD COLUMN signature_file_name TEXT")
        self.db.execute("""CREATE TABLE IF NOT EXISTS deletion_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,
          emaster_id TEXT NOT NULL, activity_date TEXT NOT NULL,
          activity TEXT NOT NULL, object_work TEXT NOT NULL,
          deleted_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS edit_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,
          emaster_id TEXT NOT NULL, activity_date_before TEXT NOT NULL,
          activity_date_after TEXT NOT NULL, activity_before TEXT NOT NULL,
          activity_after TEXT NOT NULL, edited_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        # Nama tabel khusus v21 menghindari benturan dengan tabel ``favorites``
        # versi bot lama yang memiliki kolom dan UNIQUE constraint berbeda.
        self.db.execute("""CREATE TABLE IF NOT EXISTS employee_favorites (
          id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,
          code TEXT NOT NULL, activity TEXT NOT NULL, unit TEXT NOT NULL,
          wpt INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(telegram_id,code,activity)
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS drafts (
          telegram_id INTEGER PRIMARY KEY, payload_json TEXT NOT NULL,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS daily_activities (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          telegram_id INTEGER NOT NULL,
          employee_type TEXT NOT NULL CHECK(employee_type IN ('asn','non_asn')),
          activity_date TEXT NOT NULL,
          activity_time TEXT NOT NULL,
          activity_text TEXT NOT NULL,
          emaster_status TEXT NOT NULL DEFAULT 'not_required',
          emaster_code TEXT,
          emaster_activity TEXT,
          emaster_target TEXT,
          wpt_minutes INTEGER NOT NULL DEFAULT 0,
          source_chat_id INTEGER,
          source_message_id INTEGER,
          created_at_local TEXT NOT NULL,
          updated_at_local TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          UNIQUE(telegram_id,source_chat_id,source_message_id)
        )""")
        self.db.execute("""CREATE INDEX IF NOT EXISTS idx_daily_user_date
          ON daily_activities(telegram_id,activity_date,status)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS activity_evidence (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          daily_activity_id INTEGER NOT NULL,
          telegram_file_id TEXT,
          telegram_unique_id TEXT,
          mime_type TEXT,
          file_name TEXT,
          drive_file_id TEXT,
          drive_url TEXT,
          upload_status TEXT NOT NULL DEFAULT 'pending',
          upload_error TEXT,
          created_at_local TEXT NOT NULL,
          FOREIGN KEY(daily_activity_id) REFERENCES daily_activities(id),
          UNIQUE(daily_activity_id,telegram_unique_id)
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS wfh_reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          telegram_id INTEGER NOT NULL,
          report_date TEXT NOT NULL,
          file_name TEXT NOT NULL,
          drive_file_id TEXT,
          drive_url TEXT,
          pdf_file_name TEXT,
          pdf_drive_file_id TEXT,
          pdf_drive_url TEXT,
          activity_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending',
          generated_at_local TEXT,
          error_message TEXT,
          UNIQUE(telegram_id,report_date)
        )""")
        report_columns = {row[1] for row in self.db.execute("PRAGMA table_info(wfh_reports)")}
        if "pdf_file_name" not in report_columns:
            self.db.execute("ALTER TABLE wfh_reports ADD COLUMN pdf_file_name TEXT")
        if "pdf_drive_file_id" not in report_columns:
            self.db.execute("ALTER TABLE wfh_reports ADD COLUMN pdf_drive_file_id TEXT")
        if "pdf_drive_url" not in report_columns:
            self.db.execute("ALTER TABLE wfh_reports ADD COLUMN pdf_drive_url TEXT")
        self.db.execute("""CREATE TABLE IF NOT EXISTS bot_settings (
          setting_key TEXT PRIMARY KEY,
          setting_value TEXT NOT NULL,
          updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        self._migrate_legacy_favorites()
        self.db.commit()
        try:
            Path(path).chmod(0o600)
        except OSError:
            pass

    def add_sent(self, telegram_id, date, item, volume, object_work):
        self.db.execute("""INSERT INTO activities
          (telegram_id,activity_date,code,activity,unit,wpt,volume,object_work,status,sent_at)
          VALUES (?,?,?,?,?,?,?,?,'sent',CURRENT_TIMESTAMP)""",
          (telegram_id, date, item.code, item.activity, item.unit, item.wpt, volume, object_work))
        self.db.commit()

    def month_total(self, month_year: str) -> tuple[int, int]:
        row = self.db.execute("""SELECT COUNT(*), COALESCE(SUM(wpt*volume),0)
          FROM activities WHERE status='sent' AND substr(activity_date,4,7)=?""", (month_year,)).fetchone()
        return int(row[0]), int(row[1])

    def recent(self, telegram_id: int, limit: int = 8):
        return self.db.execute("""SELECT activity_date, activity, wpt, volume, object_work
          FROM activities WHERE status='sent' AND telegram_id=? ORDER BY id DESC LIMIT ?""",
          (telegram_id, limit)).fetchall()

    def add_deleted(self, telegram_id: int, activity):
        self.db.execute("""INSERT INTO deletion_audit
          (telegram_id,emaster_id,activity_date,activity,object_work)
          VALUES (?,?,?,?,?)""",
          (telegram_id, activity.id_realisasi, activity.date,
           activity.detail, activity.object_work))
        self.db.commit()

    def add_edited(self, telegram_id: int, before, after):
        self.db.execute("""INSERT INTO edit_audit
          (telegram_id,emaster_id,activity_date_before,activity_date_after,
           activity_before,activity_after) VALUES (?,?,?,?,?,?)""",
          (telegram_id, before.id_realisasi, before.date, after.date,
           before.detail, after.detail))
        self.db.commit()

    def add_favorite(self, telegram_id: int, item):
        self.db.execute("""INSERT INTO employee_favorites(telegram_id,code,activity,unit,wpt)
          VALUES(?,?,?,?,?) ON CONFLICT(telegram_id,code,activity) DO UPDATE SET
          unit=excluded.unit,wpt=excluded.wpt""",
          (telegram_id, item.code, item.activity, item.unit, item.wpt))
        self.db.commit()

    def list_favorites(self, telegram_id: int, limit: int = 20):
        return self.db.execute("""SELECT id,code,activity,unit,wpt
          FROM employee_favorites WHERE telegram_id=? ORDER BY activity COLLATE NOCASE LIMIT ?""",
          (telegram_id, limit)).fetchall()

    def get_favorite(self, telegram_id: int, favorite_id: int):
        return self.db.execute("""SELECT id,code,activity,unit,wpt
          FROM employee_favorites WHERE telegram_id=? AND id=?""",
          (telegram_id, favorite_id)).fetchone()

    def delete_favorite(self, telegram_id: int, favorite_id: int):
        self.db.execute("DELETE FROM employee_favorites WHERE telegram_id=? AND id=?",
                        (telegram_id, favorite_id))
        self.db.commit()

    def _migrate_legacy_favorites(self):
        """Salin favorit lama tanpa mengubah atau menghapus tabel sumbernya."""
        table = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='favorites'").fetchone()
        if not table:
            return
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(favorites)")}
        required = {"code", "activity", "unit", "wpt"}
        if not required.issubset(columns):
            return
        if "telegram_id" in columns:
            self.db.execute("""INSERT OR IGNORE INTO employee_favorites
              (telegram_id,code,activity,unit,wpt)
              SELECT telegram_id,code,activity,unit,wpt FROM favorites""")
        else:
            self.db.execute("""INSERT OR IGNORE INTO employee_favorites
              (telegram_id,code,activity,unit,wpt)
              SELECT 0,code,activity,unit,wpt FROM favorites""")

    def claim_legacy_favorites(self, admin_id: int):
        self.db.execute(
            "UPDATE employee_favorites SET telegram_id=? WHERE telegram_id=0", (admin_id,))
        self.db.commit()

    def save_draft(self, telegram_id: int, payload_json: str):
        self.db.execute("""INSERT INTO drafts(telegram_id,payload_json,updated_at)
          VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(telegram_id) DO UPDATE SET
          payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP""",
          (telegram_id, payload_json))
        self.db.commit()

    def get_draft(self, telegram_id: int):
        row = self.db.execute("SELECT payload_json,updated_at FROM drafts WHERE telegram_id=?",
                              (telegram_id,)).fetchone()
        return row

    def delete_draft(self, telegram_id: int):
        self.db.execute("DELETE FROM drafts WHERE telegram_id=?", (telegram_id,))
        self.db.commit()

    def claim_legacy_activities(self, admin_id: int):
        self.db.execute("UPDATE activities SET telegram_id=? WHERE telegram_id=0", (admin_id,))
        self.db.commit()

    def invite_user(self, telegram_id: int, nip: str, full_name: str = "",
                    employee_type: str = "asn",
                    unit_name: str = "Bidang Pemasaran dan Kelembagaan Parekraf"):
        if employee_type not in {"asn", "non_asn"}:
            raise ValueError("Jenis pegawai tidak valid")
        status = "active" if employee_type == "non_asn" else "invited"
        self.db.execute("""INSERT INTO users(telegram_id,nip,full_name,status)
          VALUES(?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET
          nip=excluded.nip, full_name=excluded.full_name, status=excluded.status,
          employee_type=?, unit_name=?, drive_folder_id=NULL,
          signature_drive_file_id=NULL,signature_drive_url=NULL,signature_file_name=NULL,
          password_enc=NULL, position=NULL, profile_updated_at=NULL""",
          (telegram_id, nip, full_name, status, employee_type, unit_name))
        # Kolom baru tidak terdapat pada daftar INSERT lama agar migrasi tetap aman.
        self.db.execute("""UPDATE users SET employee_type=?, unit_name=?
          WHERE telegram_id=?""", (employee_type, unit_name, telegram_id))
        self.db.commit()

    def ensure_admin(self, telegram_id: int, nip: str, password_enc: str):
        self.db.execute("""INSERT INTO users(telegram_id,nip,password_enc,status,is_admin)
          VALUES(?,?,?,'active',1) ON CONFLICT(telegram_id) DO UPDATE SET
          nip=excluded.nip, password_enc=excluded.password_enc,
          status='active', is_admin=1, employee_type='asn'""",
          (telegram_id, nip, password_enc))
        self.db.commit()

    def get_user(self, telegram_id: int):
        return self.db.execute("""SELECT telegram_id,nip,password_enc,full_name,status,is_admin,
          position,profile_updated_at,employee_type,unit_name,drive_folder_id,
          signature_drive_file_id,signature_drive_url,signature_file_name
          FROM users WHERE telegram_id=?""", (telegram_id,)).fetchone()

    def update_profile(self, telegram_id: int, full_name: str, position: str):
        self.db.execute("""UPDATE users SET
          full_name=CASE WHEN ?<>'' THEN ? ELSE full_name END,
          position=CASE WHEN ?<>'' THEN ? ELSE position END,
          profile_updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?""",
          (full_name, full_name, position, position, telegram_id))
        self.db.commit()

    def clear_profile_position(self, telegram_id: int):
        self.db.execute("""UPDATE users SET position=NULL,
          profile_updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?""",
          (telegram_id,))
        self.db.commit()

    def activate_user(self, telegram_id: int, password_enc: str):
        self.db.execute("UPDATE users SET password_enc=?, status='active' WHERE telegram_id=? AND status='invited'",
                        (password_enc, telegram_id))
        self.db.commit()

    def list_users(self):
        return self.db.execute("""SELECT telegram_id,nip,full_name,status,is_admin,employee_type
          FROM users ORDER BY is_admin DESC, full_name, telegram_id""").fetchall()

    def disable_user(self, telegram_id: int):
        self.db.execute("UPDATE users SET status='disabled' WHERE telegram_id=? AND is_admin=0", (telegram_id,))
        self.db.commit()

    def set_drive_folder_id(self, telegram_id: int, folder_id: str):
        with self.lock:
            self.db.execute("UPDATE users SET drive_folder_id=? WHERE telegram_id=?",
                            (folder_id, telegram_id))
            self.db.commit()

    def set_signature(self, telegram_id: int, *, drive_file_id: str,
                      drive_url: str, file_name: str):
        with self.lock:
            self.db.execute("""UPDATE users SET signature_drive_file_id=?,
              signature_drive_url=?,signature_file_name=? WHERE telegram_id=?""",
              (drive_file_id, drive_url, file_name, telegram_id))
            self.db.commit()

    def add_daily_activity(self, *, telegram_id: int, employee_type: str,
                           activity_date: str, activity_time: str, activity_text: str,
                           created_at_local: str, emaster_status: str = "not_required",
                           emaster_code: str | None = None,
                           emaster_activity: str | None = None,
                           emaster_target: str | None = None,
                           wpt_minutes: int = 0, source_chat_id: int | None = None,
                           source_message_id: int | None = None) -> tuple[int, bool]:
        """Simpan jurnal harian secara idempoten berdasarkan pesan Telegram sumber."""
        with self.lock:
            try:
                cursor = self.db.execute("""INSERT INTO daily_activities
                  (telegram_id,employee_type,activity_date,activity_time,activity_text,
                   emaster_status,emaster_code,emaster_activity,emaster_target,wpt_minutes,
                   source_chat_id,source_message_id,created_at_local,updated_at_local)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (telegram_id, employee_type, activity_date, activity_time, activity_text,
                   emaster_status, emaster_code, emaster_activity, emaster_target,
                   int(wpt_minutes), source_chat_id, source_message_id,
                   created_at_local, created_at_local))
                self.db.commit()
                return int(cursor.lastrowid), True
            except sqlite3.IntegrityError:
                if source_chat_id is None or source_message_id is None:
                    raise
                row = self.db.execute("""SELECT id FROM daily_activities
                  WHERE telegram_id=? AND source_chat_id=? AND source_message_id=?""",
                  (telegram_id, source_chat_id, source_message_id)).fetchone()
                if not row:
                    raise
                return int(row[0]), False

    def add_evidence(self, *, daily_activity_id: int, telegram_file_id: str,
                     telegram_unique_id: str, mime_type: str, file_name: str,
                     created_at_local: str) -> int:
        with self.lock:
            self.db.execute("""INSERT OR IGNORE INTO activity_evidence
              (daily_activity_id,telegram_file_id,telegram_unique_id,mime_type,file_name,
               created_at_local) VALUES(?,?,?,?,?,?)""",
              (daily_activity_id, telegram_file_id, telegram_unique_id, mime_type,
               file_name, created_at_local))
            row = self.db.execute("""SELECT id FROM activity_evidence
              WHERE daily_activity_id=? AND telegram_unique_id=?""",
              (daily_activity_id, telegram_unique_id)).fetchone()
            self.db.commit()
            return int(row[0])

    def mark_evidence_uploaded(self, evidence_id: int, drive_file_id: str,
                               drive_url: str, file_name: str):
        with self.lock:
            self.db.execute("""UPDATE activity_evidence SET drive_file_id=?,drive_url=?,
              file_name=?,upload_status='uploaded',upload_error=NULL WHERE id=?""",
              (drive_file_id, drive_url, file_name, evidence_id))
            self.db.commit()

    def mark_evidence_failed(self, evidence_id: int, error_message: str):
        with self.lock:
            self.db.execute("""UPDATE activity_evidence SET upload_status='failed',
              upload_error=? WHERE id=?""", (error_message[:500], evidence_id))
            self.db.commit()

    def get_evidence(self, evidence_id: int):
        return self.db.execute("""SELECT e.id,e.daily_activity_id,e.telegram_file_id,
          e.telegram_unique_id,e.mime_type,e.file_name,e.drive_file_id,e.drive_url,
          e.upload_status,a.telegram_id,a.activity_date,a.activity_time,a.activity_text
          FROM activity_evidence e JOIN daily_activities a ON a.id=e.daily_activity_id
          WHERE e.id=?""", (evidence_id,)).fetchone()

    def list_daily_activities(self, telegram_id: int, *, limit: int = 20,
                              activity_date: str | None = None):
        params: list[object] = [telegram_id]
        where = "a.telegram_id=? AND a.status='active'"
        if activity_date:
            where += " AND a.activity_date=?"
            params.append(activity_date)
        params.append(limit)
        return self.db.execute(f"""SELECT a.id,a.activity_date,a.activity_time,
          a.activity_text,a.employee_type,a.emaster_status,a.wpt_minutes,
          COUNT(e.id) AS evidence_count,
          SUM(CASE WHEN e.upload_status='uploaded' THEN 1 ELSE 0 END) AS uploaded_count
          FROM daily_activities a LEFT JOIN activity_evidence e ON e.daily_activity_id=a.id
          WHERE {where} GROUP BY a.id ORDER BY a.activity_date DESC,a.activity_time DESC,a.id DESC
          LIMIT ?""", params).fetchall()

    def list_wfh_activities(self, telegram_id: int, report_date: str):
        """Ambil hanya aktivitas yang tanggal dan waktu inputnya berada pada Jumat yang sama."""
        with self.lock:
            rows = self.db.execute("""SELECT id,activity_date,activity_time,activity_text,
              employee_type,emaster_status,wpt_minutes,created_at_local
              FROM daily_activities WHERE telegram_id=? AND activity_date=?
              AND substr(created_at_local,1,10)=? AND status='active'
              ORDER BY activity_time,id""", (telegram_id, report_date, report_date)).fetchall()
            result = []
            for row in rows:
                evidence = self.db.execute("""SELECT id,file_name,drive_file_id,drive_url,mime_type
                  FROM activity_evidence WHERE daily_activity_id=? AND upload_status='uploaded'
                  ORDER BY id""", (row[0],)).fetchall()
                result.append((row, evidence))
            return result

    def mark_daily_deleted(self, telegram_id: int, activity_date: str,
                           emaster_activity: str, activity_text: str):
        with self.lock:
            row = self.db.execute("""SELECT id FROM daily_activities
              WHERE telegram_id=? AND activity_date=? AND emaster_activity=?
              AND activity_text=? AND status='active' ORDER BY id DESC LIMIT 1""",
              (telegram_id, activity_date, emaster_activity, activity_text)).fetchone()
            if row:
                self.db.execute("""UPDATE daily_activities SET status='deleted',
                  updated_at_local=datetime('now') WHERE id=?""", (row[0],))
                self.db.commit()

    def sync_daily_edit(self, telegram_id: int, *, before_date: str,
                        before_activity: str, before_text: str,
                        after_date: str, after_activity: str, after_text: str,
                        after_wpt_minutes: int):
        with self.lock:
            row = self.db.execute("""SELECT id FROM daily_activities
              WHERE telegram_id=? AND activity_date=? AND emaster_activity=?
              AND activity_text=? AND status='active' ORDER BY id DESC LIMIT 1""",
              (telegram_id, before_date, before_activity, before_text)).fetchone()
            if row:
                self.db.execute("""UPDATE daily_activities SET activity_date=?,
                  emaster_activity=?,activity_text=?,wpt_minutes=?,
                  updated_at_local=datetime('now') WHERE id=?""",
                  (after_date, after_activity, after_text, int(after_wpt_minutes), row[0]))
                self.db.commit()

    def recent_evidence(self, telegram_id: int, limit: int = 10):
        return self.db.execute("""SELECT a.activity_date,a.activity_text,e.file_name,e.drive_url,
          e.upload_status,e.id FROM activity_evidence e
          JOIN daily_activities a ON a.id=e.daily_activity_id
          WHERE a.telegram_id=? AND a.status='active'
          ORDER BY e.id DESC LIMIT ?""", (telegram_id, limit)).fetchall()

    def get_report(self, telegram_id: int, report_date: str):
        with self.lock:
            return self.db.execute("""SELECT id,file_name,drive_file_id,drive_url,
              activity_count,status,generated_at_local,error_message,
              pdf_file_name,pdf_drive_file_id,pdf_drive_url FROM wfh_reports
              WHERE telegram_id=? AND report_date=?""", (telegram_id, report_date)).fetchone()

    def latest_report(self, telegram_id: int):
        return self.db.execute("""SELECT report_date,file_name,drive_url,activity_count,status,
          generated_at_local,pdf_file_name,pdf_drive_url
          FROM wfh_reports WHERE telegram_id=? AND status='uploaded'
          ORDER BY report_date DESC LIMIT 1""", (telegram_id,)).fetchone()

    def save_report(self, *, telegram_id: int, report_date: str, file_name: str,
                    drive_file_id: str, drive_url: str, activity_count: int,
                    generated_at_local: str, pdf_file_name: str,
                    pdf_drive_file_id: str, pdf_drive_url: str):
        with self.lock:
            self.db.execute("""INSERT INTO wfh_reports
              (telegram_id,report_date,file_name,drive_file_id,drive_url,
               pdf_file_name,pdf_drive_file_id,pdf_drive_url,activity_count,
               status,generated_at_local,error_message)
              VALUES(?,?,?,?,?,?,?,?,?,'uploaded',?,NULL)
              ON CONFLICT(telegram_id,report_date) DO UPDATE SET
              file_name=excluded.file_name,drive_file_id=excluded.drive_file_id,
              drive_url=excluded.drive_url,pdf_file_name=excluded.pdf_file_name,
              pdf_drive_file_id=excluded.pdf_drive_file_id,
              pdf_drive_url=excluded.pdf_drive_url,
              activity_count=excluded.activity_count,
              status='uploaded',generated_at_local=excluded.generated_at_local,
              error_message=NULL""",
              (telegram_id, report_date, file_name, drive_file_id, drive_url,
               pdf_file_name, pdf_drive_file_id, pdf_drive_url,
               activity_count, generated_at_local))
            self.db.commit()

    def save_report_error(self, telegram_id: int, report_date: str, file_name: str,
                          error_message: str):
        with self.lock:
            self.db.execute("""INSERT INTO wfh_reports
              (telegram_id,report_date,file_name,status,error_message)
              VALUES(?,?,?,'failed',?) ON CONFLICT(telegram_id,report_date) DO UPDATE SET
              file_name=excluded.file_name,status='failed',error_message=excluded.error_message""",
              (telegram_id, report_date, file_name, error_message[:500]))
            self.db.commit()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT setting_value FROM bot_settings WHERE setting_key=?",
                              (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str):
        with self.lock:
            self.db.execute("""INSERT INTO bot_settings(setting_key,setting_value,updated_at)
              VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(setting_key) DO UPDATE SET
              setting_value=excluded.setting_value,updated_at=CURRENT_TIMESTAMP""", (key, value))
            self.db.commit()

    def maintenance_active(self) -> bool:
        return self.get_setting("maintenance_active", "0") == "1"

    def wfh_report_access_mode(self) -> str:
        mode = self.get_setting("wfh_report_access_mode", "auto") or "auto"
        return mode if mode in {"auto", "open", "closed"} else "auto"

    def set_wfh_report_access_mode(self, mode: str):
        if mode not in {"auto", "open", "closed"}:
            raise ValueError("Mode akses laporan WFH tidak valid")
        self.set_setting("wfh_report_access_mode", mode)

    def list_notification_user_ids(self):
        return [int(row[0]) for row in self.db.execute(
            "SELECT telegram_id FROM users WHERE status='active' ORDER BY telegram_id").fetchall()]
