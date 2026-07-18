import sqlite3
from pathlib import Path


class Storage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS activities (
          id INTEGER PRIMARY KEY AUTOINCREMENT, activity_date TEXT NOT NULL,
          code TEXT NOT NULL, activity TEXT NOT NULL, unit TEXT NOT NULL,
          wpt INTEGER NOT NULL, volume INTEGER NOT NULL, object_work TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          sent_at TEXT
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS favorites (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, activity TEXT NOT NULL,
          unit TEXT NOT NULL, wpt INTEGER NOT NULL, object_work TEXT NOT NULL,
          UNIQUE(code, object_work)
        )""")
        self.db.commit()

    def add_sent(self, date, item, volume, object_work):
        self.db.execute("""INSERT INTO activities
          (activity_date,code,activity,unit,wpt,volume,object_work,status,sent_at)
          VALUES (?,?,?,?,?,?,?,'sent',CURRENT_TIMESTAMP)""",
          (date, item.code, item.activity, item.unit, item.wpt, volume, object_work))
        self.db.commit()

    def month_total(self, month_year: str) -> tuple[int, int]:
        row = self.db.execute("""SELECT COUNT(*), COALESCE(SUM(wpt*volume),0)
          FROM activities WHERE status='sent' AND substr(activity_date,4,7)=?""", (month_year,)).fetchone()
        return int(row[0]), int(row[1])

    def recent(self, limit=10):
        return self.db.execute("""SELECT id,activity_date,code,activity,unit,wpt,volume,object_work
          FROM activities WHERE status='sent' ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()

    def add_favorite(self, code, activity, unit, wpt, object_work):
        self.db.execute("""INSERT OR REPLACE INTO favorites(code,activity,unit,wpt,object_work)
          VALUES(?,?,?,?,?)""", (code, activity, unit, wpt, object_work))
        self.db.commit()

    def favorites(self):
        return self.db.execute("SELECT id,code,activity,unit,wpt,object_work FROM favorites ORDER BY activity").fetchall()

    def delete_favorite(self, favorite_id):
        self.db.execute("DELETE FROM favorites WHERE id=?", (favorite_id,))
        self.db.commit()
