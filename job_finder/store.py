import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .migrate import DEFAULT_SETTINGS, backup_to, migrate
from .models import Letter, Vacancy

# Целевые статусы (раздел 5 плана): статусы отправки удалены вместе
# с автооткликом (1b); их данные переносятся в архив миграцией 1c.
STATES = {"found", "read", "rejected", "skipped", "needs_clarification",
          "ready", "delivered", "closed", "error"}
# Терминальные статусы: автоматика их не перезаписывает.
TERMINAL = ("skipped", "closed")

CREATE_V4 = '''
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '', letter TEXT,
  created TEXT NOT NULL, updated TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'hh', native_id TEXT, published_at TEXT,
  cluster_id INTEGER, last_seen TEXT, last_read TEXT,
  norm_company TEXT, norm_title TEXT, content_hash TEXT,
  test_required INTEGER NOT NULL DEFAULT 0,
  grade TEXT, grade_evidence TEXT, skills TEXT,
  rules_version TEXT, profile_version TEXT,
  manual_decision TEXT, manual_decision_at TEXT);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE inbox (update_id INTEGER PRIMARY KEY, command TEXT, created TEXT NOT NULL);
CREATE TABLE outbox (id INTEGER PRIMARY KEY, text TEXT NOT NULL,
  sent INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'event', job_id TEXT, cluster_id TEXT,
  legacy INTEGER NOT NULL DEFAULT 0);
CREATE TABLE skip_events (id INTEGER PRIMARY KEY, job_id TEXT NOT NULL,
  reason TEXT NOT NULL, created TEXT NOT NULL, reported INTEGER NOT NULL DEFAULT 0);
CREATE TABLE application_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  origin TEXT NOT NULL,
  job_id TEXT NOT NULL,
  status TEXT NOT NULL,
  started TEXT,
  ended TEXT,
  evidence TEXT,
  reason TEXT,
  letter TEXT,
  data TEXT,
  created TEXT NOT NULL);
CREATE TABLE clusters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rep_job_id TEXT,
  centroid BLOB,
  embedding_model TEXT,
  embedding_dim INTEGER,
  created TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'none',
  skipped_at TEXT);
CREATE TABLE job_embeddings (
  job_id TEXT PRIMARY KEY,
  embedding BLOB,
  model TEXT,
  dim INTEGER,
  content_hash TEXT);
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  kind TEXT,
  model TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cost REAL,
  created TEXT NOT NULL);
CREATE INDEX idx_jobs_norm_company ON jobs(norm_company);
CREATE INDEX idx_jobs_norm_title ON jobs(norm_title);
CREATE INDEX idx_jobs_cluster_id ON jobs(cluster_id);
CREATE UNIQUE INDEX idx_outbox_card_cluster ON outbox(cluster_id)
  WHERE kind='card' AND cluster_id IS NOT NULL;
CREATE UNIQUE INDEX idx_outbox_card_job ON outbox(job_id)
  WHERE kind='card' AND job_id IS NOT NULL;
'''

PILOT_REASON = "отклик уже отправлен ранее (архив)"


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
        if version not in (0, 1, 2, 3, 4):
            self.conn.close()
            raise ValueError("База создана более новой версией приложения")
        existing = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if version < 4:
            if "jobs" in existing:
                # Миграция 3→4 (контракт R6): свежий бэкап ПЕРЕД изменениями.
                backup_to(path, path.parent / "backups")
                migrate(self.conn)
            else:
                # Свежая база создаётся сразу в целевой схеме.
                self.conn.executescript(CREATE_V4)
                self.conn.execute("PRAGMA user_version=4")
        # Сиды (настройки по умолчанию и пилот) — только для новой базы;
        # существующая БД уже просидирована, и повторные записи не нужны
        # (заодно не берут блокировку записи при каждом открытии).
        seeded = self.conn.execute("SELECT 1 FROM jobs WHERE id='hh:137924141'").fetchone()
        if seeded is None:
            with self.conn:
                for key, value in DEFAULT_SETTINGS.items():
                    self.conn.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, dump(value)))
                pilot = Vacancy("137924141", "Инженер поддержки и развития (LLM-агенты)",
                                "Python", company="Ат Проф", remote=True)
                self.conn.execute("INSERT OR IGNORE INTO jobs(id,data,status,reason,created,updated)"
                                  " VALUES(?,?,'skipped',?,?,?)",
                                  ("hh:137924141", dump(pilot.to_dict()), PILOT_REASON, now(), now()))

    @staticmethod
    def key(job_id):
        """Ключ источника: source:native_id; hh — единственный источник."""
        job_id = str(job_id)
        return job_id if ":" in job_id else "hh:" + job_id

    @staticmethod
    def native(job_id):
        job_id = str(job_id)
        return job_id.split(":", 1)[1] if ":" in job_id else job_id

    def close(self):
        self.conn.close()

    def get(self, job_id):
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (self.key(job_id),)).fetchone()
        if row is None:
            return None
        row = dict(row)
        data = json.loads(row.pop("data"))
        row["possible_duplicates"] = data.pop("possible_duplicates", None)
        row["llm"] = data.pop("llm", None)
        row["vacancy"] = Vacancy.from_dict(data)
        row["letter"] = Letter(**json.loads(row["letter"])) if row["letter"] else None
        return row

    def discover(self, vacancy):
        with self.conn:
            cursor=self.conn.execute("INSERT OR IGNORE INTO jobs(id,data,status,created,updated) VALUES(?,?,'found',?,?)", (self.key(vacancy.id),dump(vacancy.to_dict()),now(),now()))
        return bool(cursor.rowcount)

    def refresh(self, vacancy):
        with self.conn:
            self.conn.execute("UPDATE jobs SET data=?,updated=? WHERE id=?",(dump(vacancy.to_dict()),now(),self.key(vacancy.id)))

    def decide(self, job_id, status, reason, grade=None, grade_evidence=None, rules_version=None):
        if status not in STATES:
            raise ValueError("Неизвестный статус")
        with self.conn:
            previous=self.conn.execute("SELECT status,reason FROM jobs WHERE id=?",(self.key(job_id),)).fetchone()
            placeholders=",".join("?" for _ in TERMINAL)
            cursor=self.conn.execute("UPDATE jobs SET status=?,reason=?,grade=?,grade_evidence=?,rules_version=?,updated=? WHERE id=? AND status NOT IN ("+placeholders+")",(status,reason,grade,grade_evidence,rules_version,now(),self.key(job_id),*TERMINAL))
            if cursor.rowcount:
                self._record_skip(job_id,status,reason,previous)

    def make_ready(self, job_id, letter, reason, rules_version=None):
        with self.conn:
            self.conn.execute("UPDATE jobs SET letter=?,status='ready',reason=?,rules_version=?,updated=? WHERE id=? AND status='found'",(dump(letter.to_dict()) if letter is not None else None,reason,rules_version,now(),self.key(job_id)))

    def save_letter(self, job_id, letter):
        with self.conn:
            placeholders=",".join("?" for _ in TERMINAL)
            self.conn.execute("UPDATE jobs SET letter=?,updated=? WHERE id=? AND status NOT IN ("+placeholders+")",(dump(letter.to_dict()),now(),self.key(job_id),*TERMINAL))

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
