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
        if version not in (0, 1, 2, 3):
            self.conn.close()
            raise ValueError("База создана более новой версией приложения")
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL,
          reason TEXT NOT NULL DEFAULT '', letter TEXT, resume_id TEXT,
          evidence TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inbox (update_id INTEGER PRIMARY KEY, command TEXT, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, text TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'event');
        CREATE TABLE IF NOT EXISTS skip_events (id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, reason TEXT NOT NULL, created TEXT NOT NULL, reported INTEGER NOT NULL DEFAULT 0);
        ''')
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if "kind" not in {row[1] for row in self.conn.execute("PRAGMA table_info(outbox)")}:
                self.conn.execute("ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'event'")
                prefix="Отклик подтверждён: "
                self.conn.execute("UPDATE outbox SET kind='success' WHERE substr(text,1,?)=?",(len(prefix),prefix))
            self.conn.execute("PRAGMA user_version=3")
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
            previous=self.conn.execute("SELECT status,reason FROM jobs WHERE id=?",(job_id,)).fetchone()
            cursor=self.conn.execute("UPDATE jobs SET status=?,reason=?,updated=? WHERE id=? AND status NOT IN ('sent','sending','unknown')",(status,reason,now(),job_id))
            if cursor.rowcount:
                self._record_skip(job_id,status,reason,previous)

    def make_ready(self, job_id, letter, reason):
        with self.conn:
            self.conn.execute("UPDATE jobs SET letter=?,status='ready',reason=?,updated=? WHERE id=? AND status='found'",(dump(letter.to_dict()),reason,now(),job_id))

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
        """Пауза доставки: пока включена, уведомления не отправляются в Telegram."""
        self.set("paused",paused)

    def is_paused(self):
        return self.setting("paused",True)

    def _record_skip(self, job_id, status, reason, previous):
        if status=='rejected' and 'предложено пройти тест' in reason and (not previous or previous[0]!='rejected' or previous[1]!=reason):
            self.conn.execute("INSERT INTO skip_events(job_id,reason,created) VALUES(?,?,?)",(job_id,reason,now()))

    def pending_skips(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM skip_events WHERE reported=0 ORDER BY id")]

    def queue_digest(self, text, day, skip_ids):
        with self.conn:
            self._enqueue(text)
            self.conn.executemany("UPDATE skip_events SET reported=1 WHERE id=?",[(event_id,) for event_id in skip_ids])
            self._set('digest_day',day)
            self._set('last_digest_at',now())

    def rows(self, statuses=None, limit=50, newest=False, offset=0):
        sql="SELECT id FROM jobs"
        args=[]
        if statuses:
            sql += " WHERE status IN ("+",".join("?" for _ in statuses)+")"
            args.extend(statuses)
        sql += " ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?" if newest else " ORDER BY created,id LIMIT ? OFFSET ?"
        args.extend((limit,offset))
        return [self.get(row[0]) for row in self.conn.execute(sql,args).fetchall()]

    def counts(self):
        return dict(self.conn.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status").fetchall())

    def _enqueue(self, text, kind="event"):
        if kind not in ("event","reply","success"):
            raise ValueError("Неизвестный тип уведомления")
        self.conn.execute("INSERT INTO outbox(text,created,kind) VALUES(?,?,?)",(text,now(),kind))

    def enqueue(self, text, kind="event"):
        with self.conn:
            self._enqueue(text,kind=kind)

    def pending_messages(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM outbox WHERE sent=0 AND kind IN ('reply','success') ORDER BY id LIMIT 20")]

    def delivered(self, message_id):
        with self.conn:
            self.conn.execute("UPDATE outbox SET sent=1 WHERE id=?",(message_id,))

    def command(self, update_id, action, response=None):
        """Record a Telegram report request without changing worker state."""
        with self.conn:
            cursor=self.conn.execute("INSERT OR IGNORE INTO inbox VALUES(?,?,?)",(update_id,action,now()))
            if not cursor.rowcount:
                return False
            self._set("telegram_offset",update_id+1)
            if response:
                self._enqueue(response,kind="reply")
        return True
