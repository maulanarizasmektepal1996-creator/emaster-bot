import sqlite3
from pathlib import Path


class Storage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
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
          is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
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

    def claim_legacy_activities(self, admin_id: int):
        self.db.execute("UPDATE activities SET telegram_id=? WHERE telegram_id=0", (admin_id,))
        self.db.commit()

    def invite_user(self, telegram_id: int, nip: str, full_name: str = ""):
        self.db.execute("""INSERT INTO users(telegram_id,nip,full_name,status)
          VALUES(?,?,?,'invited') ON CONFLICT(telegram_id) DO UPDATE SET
          nip=excluded.nip, full_name=excluded.full_name, status='invited', password_enc=NULL""",
          (telegram_id, nip, full_name))
        self.db.commit()

    def ensure_admin(self, telegram_id: int, nip: str, password_enc: str):
        self.db.execute("""INSERT INTO users(telegram_id,nip,password_enc,status,is_admin)
          VALUES(?,?,?,'active',1) ON CONFLICT(telegram_id) DO UPDATE SET
          nip=excluded.nip, password_enc=excluded.password_enc,
          status='active', is_admin=1""", (telegram_id, nip, password_enc))
        self.db.commit()

    def get_user(self, telegram_id: int):
        return self.db.execute("""SELECT telegram_id,nip,password_enc,full_name,status,is_admin
          FROM users WHERE telegram_id=?""", (telegram_id,)).fetchone()

    def activate_user(self, telegram_id: int, password_enc: str):
        self.db.execute("UPDATE users SET password_enc=?, status='active' WHERE telegram_id=? AND status='invited'",
                        (password_enc, telegram_id))
        self.db.commit()

    def list_users(self):
        return self.db.execute("""SELECT telegram_id,nip,full_name,status,is_admin
          FROM users ORDER BY is_admin DESC, full_name, telegram_id""").fetchall()

    def disable_user(self, telegram_id: int):
        self.db.execute("UPDATE users SET status='disabled' WHERE telegram_id=? AND is_admin=0", (telegram_id,))
        self.db.commit()
