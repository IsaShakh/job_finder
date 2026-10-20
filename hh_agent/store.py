import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .models import Letter, Vacancy

STATES = {"found", "rejected", "needs_clarification", "ready", "sending", "sent", "unknown", "error", "closed"}


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=10)
        os.chmod(path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("База создана более новой версией приложения")
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL,
          reason TEXT NOT NULL DEFAULT '', letter TEXT, resume_id TEXT,
          evidence TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (
          id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, started TEXT NOT NULL,
          ended TEXT, status TEXT NOT NULL, evidence TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inbox (update_id INTEGER PRIMARY KEY, command TEXT, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, text TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
        PRAGMA user_version=1;
        ''')
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO settings VALUES ('paused','true')")
            pilot = Vacancy("137924141", "Инженер поддержки и развития (LLM-агенты)", "Python", company="Ат Проф", remote=True)
            self.conn.execute("INSERT OR IGNORE INTO jobs(id,data,status,reason,evidence,created,updated) VALUES(?,?,'sent',?,?,?,?)", (pilot.id,dump(pilot.to_dict()),"Контрольный отклик от 21.10.2026","Подтверждены история HH и чат 5727309591",now(),now()))

    def close(self):
        self.conn.close()

    def get(self, job_id):
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        row = dict(row)
        row["vacancy"] = Vacancy.from_dict(json.loads(row.pop("data")))
        row["letter"] = Letter(**json.loads(row["letter"])) if row["letter"] else None
        return row

    def discover(self, vacancy):
        with self.conn:
            cursor=self.conn.execute("INSERT OR IGNORE INTO jobs(id,data,status,created,updated) VALUES(?,?,'found',?,?)", (vacancy.id,dump(vacancy.to_dict()),now(),now()))
        return bool(cursor.rowcount)

    def refresh(self, vacancy):
        with self.conn:
            self.conn.execute("UPDATE jobs SET data=?,updated=? WHERE id=?",(dump(vacancy.to_dict()),now(),vacancy.id))

    def decide(self, job_id, status, reason):
        if status not in STATES:
            raise ValueError("Неизвестный статус")
        with self.conn:
            self.conn.execute("UPDATE jobs SET status=?,reason=?,updated=? WHERE id=? AND status NOT IN ('sent','sending','unknown')",(status,reason,now(),job_id))

    def save_letter(self, job_id, letter):
        with self.conn:
            self.conn.execute("UPDATE jobs SET letter=?,updated=? WHERE id=? AND status NOT IN ('sending','sent','unknown')",(dump(letter.to_dict()),now(),job_id))

    def setting(self, key, default=None):
        row=self.conn.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key, value):
        self.conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,dump(value)))

    def set(self, key, value):
        with self.conn:
            self._set(key,value)

    def pause(self, paused=True):
        self.set("paused",paused)

    def is_paused(self):
        return self.setting("paused",True)

    def claim(self, job_id, resume_id):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if self.is_paused():
                return False
            cursor=self.conn.execute("UPDATE jobs SET status='sending',resume_id=?,updated=? WHERE id=? AND status='ready' AND letter IS NOT NULL",(resume_id,now(),job_id))
            if cursor.rowcount:
                self.conn.execute("INSERT INTO attempts(job_id,started,status) VALUES(?,?,'sending')",(job_id,now()))
            return bool(cursor.rowcount)

    def finish(self, job_id, status, evidence="", reason="", notification=None):
        if status not in STATES or (status == "sent" and not evidence.strip()):
            raise ValueError("Успешному отклику нужно подтверждение HH")
        with self.conn:
            row=self.conn.execute("SELECT status FROM jobs WHERE id=?",(job_id,)).fetchone()
            if not row or row[0] == "sent":
                return
            if row[0] in ("sending","unknown") and status not in ("sent","unknown"):
                status,reason="unknown","Отправка начата; нужно проверить историю"
            self.conn.execute("UPDATE jobs SET status=?,evidence=?,reason=?,updated=? WHERE id=?",(status,evidence,reason,now(),job_id))
            self.conn.execute("UPDATE attempts SET status=?,evidence=?,ended=? WHERE job_id=? AND status='sending'",(status,evidence,now(),job_id))
            if notification:
                self._enqueue(notification)

    def recover(self):
        with self.conn:
            self.conn.execute("UPDATE jobs SET status='unknown',reason='Прерванная отправка: сначала проверить историю',updated=? WHERE status='sending'",(now(),))
            self.conn.execute("UPDATE attempts SET status='unknown',ended=? WHERE status='sending'",(now(),))

    def rows(self, statuses=None, limit=50):
        sql="SELECT id FROM jobs"
        args=[]
        if statuses:
            sql += " WHERE status IN ("+",".join("?" for _ in statuses)+")"
            args.extend(statuses)
        sql += " ORDER BY created,id LIMIT ?"
        args.append(limit)
        return [self.get(row[0]) for row in self.conn.execute(sql,args).fetchall()]

    def counts(self):
        return dict(self.conn.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status").fetchall())

    def _enqueue(self, text):
        self.conn.execute("INSERT INTO outbox(text,created) VALUES(?,?)",(text,now()))

    def enqueue(self, text):
        with self.conn:
            self._enqueue(text)

    def pending_messages(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT 20")]

    def delivered(self, message_id):
        with self.conn:
            self.conn.execute("UPDATE outbox SET sent=1 WHERE id=?",(message_id,))

    def command(self, update_id, action, response=None):
        with self.conn:
            cursor=self.conn.execute("INSERT OR IGNORE INTO inbox VALUES(?,?,?)",(update_id,action,now()))
            if not cursor.rowcount:
                return False
            if action in ("pause","resume"):
                self._set("paused",action=="pause")
            elif action in ("search","reconcile"):
                self._set(action+"_requested",True)
            self._set("telegram_offset",update_id+1)
            if response:
                self._enqueue(response)
        return True
