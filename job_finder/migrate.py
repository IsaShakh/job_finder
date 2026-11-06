"""Миграция схемы 1c (контракт R6, раздел 12 плана).

Идемпотентные шаги с проверкой состояния; `PRAGMA user_version` 3→4
повышается последним. Перед первым изменением данных — свежий бэкап через
`Connection.backup()` (WAL-безопасный) в `.state/backups/`.

Карта переходов старых состояний:
- `sent` → архив `application_history` → статус `skipped` («отклик уже
  отправлен ранее (архив)»);
- `unknown` → архив → `needs_clarification` («результат отправки неизвестен —
  проверьте историю hh»);
- `sending` → архив → `needs_clarification` («отправка была прервана —
  проверьте историю hh»);
- `rejected`/`needs_clarification` — остаются, `rules_version='legacy'`
  (переоценятся новыми правилами в первом раунде);
- `ready` — старое письмо снимается в `data.legacy_letter` (не используется,
  не проходило верификацию фактов), `rules_version='legacy'` → переоценка
  новыми правилами + перегенерация писем;
- `found`/`closed`/`error` — остаются как есть.

Физическое удаление старых колонок (`resume_id`, `evidence`) и таблицы
`attempts` — после переноса в архив.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone


LEGACY_KEYS = ("HH_LIVE", "next_send_at", "search_requested", "reconcile_requested")
DEFAULT_SETTINGS = {
    "paused": True,
    # Тихий режим (06.11.2026): новые вакансии не рассылаются автоматически,
    # просмотр — по запросу (/browse, /next, кнопка «Следующая вакансия»).
    "DELIVER": False,
    "NOTIFY_MODE": "immediate",
    # Описание вакансии в карточке обрезается до этого числа символов;
    # дальше — по ссылке на вакансию.
    "card_description_limit": 2000,
    # Правила v5 (06.11.2026, белый список стран + Китай): legacy-строки и
    # строки, решённые старыми версиями, переоценятся в первом раунде после
    # обновления версии.
    "rules_version": "v5",
    "profile_version": "legacy",
}

ARCHIVED_REASONS = {
    "sent": "отклик уже отправлен ранее (архив)",
    "unknown": "результат отправки неизвестен — проверьте историю hh",
    "sending": "отправка была прервана — проверьте историю hh",
}

CREATE_APPLICATION_HISTORY = """
CREATE TABLE IF NOT EXISTS application_history (
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
  created TEXT NOT NULL
)"""

CREATE_AUX_TABLES = """
CREATE TABLE IF NOT EXISTS clusters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rep_job_id TEXT,
  centroid BLOB,
  embedding_model TEXT,
  embedding_dim INTEGER,
  created TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'none',
  skipped_at TEXT
);
CREATE TABLE IF NOT EXISTS job_embeddings (
  job_id TEXT PRIMARY KEY,
  embedding BLOB,
  model TEXT,
  dim INTEGER,
  content_hash TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  kind TEXT,
  model TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cost REAL,
  created TEXT NOT NULL
)"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_jobs_norm_company ON jobs(norm_company)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_norm_title ON jobs(norm_title)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_cluster_id ON jobs(cluster_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_card_cluster ON outbox(cluster_id)"
    " WHERE kind='card' AND cluster_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_card_job ON outbox(job_id)"
    " WHERE kind='card' AND job_id IS NOT NULL",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def backup_to(path, backup_dir):
    """Свежий WAL-безопасный бэкап через Connection.backup() ДО изменений."""
    path = str(path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / ("agent-%s-v3.sqlite3" % stamp)
    source = sqlite3.connect(path)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    os.chmod(dest, 0o600)
    return dest


def migrate(conn):
    """Идемпотентная миграция 3→4; повышение user_version — последний шаг."""
    report = {"archived_attempts": 0, "archived_jobs": 0, "legacy_letters": 0}
    with conn:
        conn.execute(CREATE_APPLICATION_HISTORY)

        # 1. Архив: таблица attempts целиком (если ещё существует).
        if "attempts" in tables(conn):
            report["archived_attempts"] = conn.execute(
                "INSERT INTO application_history"
                "(origin,job_id,status,started,ended,evidence,created)"
                " SELECT 'attempts',job_id,status,started,ended,evidence,started"
                " FROM attempts").rowcount
            conn.execute("DROP TABLE attempts")

        # 2. Архив: строки jobs со статусами отправки (sent/unknown/sending).
        if "evidence" in columns(conn, "jobs"):
            report["archived_jobs"] = conn.execute(
                "INSERT INTO application_history"
                "(origin,job_id,status,evidence,reason,letter,data,created)"
                " SELECT 'jobs',id,status,evidence,reason,letter,data,updated"
                " FROM jobs WHERE status IN ('sent','unknown','sending')").rowcount
        else:
            report["archived_jobs"] = conn.execute(
                "INSERT INTO application_history"
                "(origin,job_id,status,reason,letter,data,created)"
                " SELECT 'jobs',id,status,reason,letter,data,updated"
                " FROM jobs WHERE status IN ('sent','unknown','sending')").rowcount
        for status, reason in ARCHIVED_REASONS.items():
            conn.execute("UPDATE jobs SET reason=? WHERE status=?", (reason, status))
        conn.execute("UPDATE jobs SET status='skipped' WHERE status='sent'")
        conn.execute("UPDATE jobs SET status='needs_clarification'"
                     " WHERE status IN ('unknown','sending')")

        # 3. Новые колонки jobs (целевая схема, раздел 5 плана).
        for name, ddl in {
            "source": "TEXT NOT NULL DEFAULT 'hh'",
            "native_id": "TEXT",
            "published_at": "TEXT",
            "cluster_id": "INTEGER",
            "last_seen": "TEXT",
            "last_read": "TEXT",
            "norm_company": "TEXT",
            "norm_title": "TEXT",
            "content_hash": "TEXT",
            "test_required": "INTEGER NOT NULL DEFAULT 0",
            "grade": "TEXT",
            "grade_evidence": "TEXT",
            "skills": "TEXT",
            "rules_version": "TEXT",
            "profile_version": "TEXT",
            "manual_decision": "TEXT",
            "manual_decision_at": "TEXT",
        }.items():
            if name not in columns(conn, "jobs"):
                conn.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (name, ddl))

        # 4. Ключи source:native_id: id → hh:<старый id>.
        conn.execute("UPDATE jobs SET native_id=id, source='hh' WHERE native_id IS NULL")
        conn.execute("UPDATE jobs SET id='hh:'||id WHERE id NOT GLOB 'hh:*'")
        conn.execute("UPDATE jobs SET published_at=json_extract(data,'$.published_at')"
                     " WHERE published_at IS NULL AND json_extract(data,'$.published_at') IS NOT NULL")
        conn.execute("UPDATE jobs SET test_required=1"
                     " WHERE test_required=0 AND json_extract(data,'$.test_required')=1")

        # 5. Версии правил: legacy-строки переоценятся новыми правилами.
        conn.execute("UPDATE jobs SET rules_version='legacy'"
                     " WHERE rules_version IS NULL AND status IN ('rejected','needs_clarification','ready')")

        # 6. Старые письма ready-строк — снимок в data.legacy_letter
        #    (не используются: не проходили верификацию фактов).
        for row in conn.execute("SELECT id,data,letter FROM jobs"
                                " WHERE status='ready' AND letter IS NOT NULL").fetchall():
            try:
                data = json.loads(row[1])
            except ValueError:
                continue
            if "legacy_letter" in data:
                continue
            try:
                data["legacy_letter"] = json.loads(row[2])
            except ValueError:
                continue
            conn.execute("UPDATE jobs SET data=? WHERE id=?",
                         (json.dumps(data, ensure_ascii=False), row[0]))
            report["legacy_letters"] += 1

        # 6b. Нормализация data: признак автоотклика already_applied удалён
        #     из модели в 1b — вычистить из сохранённых данных.
        for row in conn.execute("SELECT id,data FROM jobs WHERE data LIKE '%already_applied%'").fetchall():
            try:
                data = json.loads(row[1])
            except ValueError:
                continue
            if "already_applied" in data:
                data.pop("already_applied")
                conn.execute("UPDATE jobs SET data=? WHERE id=?",
                             (json.dumps(data, ensure_ascii=False), row[0]))

        # 7. Физическое удаление старых колонок — только после архива.
        for name in ("resume_id", "evidence"):
            if name in columns(conn, "jobs"):
                conn.execute("ALTER TABLE jobs DROP COLUMN " + name)

        # 8. outbox: колонки карточек + legacy-метка существующих строк.
        if "kind" not in columns(conn, "outbox"):
            conn.execute("ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'event'")
            prefix = "Отклик подтверждён: "
            conn.execute("UPDATE outbox SET kind='success' WHERE substr(text,1,?)=?",
                         (len(prefix), prefix))
        for name, ddl in {"job_id": "TEXT", "cluster_id": "TEXT",
                          "legacy": "INTEGER NOT NULL DEFAULT 0"}.items():
            if name not in columns(conn, "outbox"):
                conn.execute("ALTER TABLE outbox ADD COLUMN %s %s" % (name, ddl))
        conn.execute("UPDATE outbox SET legacy=1 WHERE legacy=0")

        # 9. Настройки: вычистить ключи отправки, добавить новые.
        for key in LEGACY_KEYS:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
        conn.execute("DELETE FROM settings WHERE key LIKE 'telegram_chunk_%'")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings VALUES (?,?)",
                         (key, json.dumps(value, ensure_ascii=False)))

        # 10. Вспомогательные таблицы целевой схемы.
        conn.executescript(CREATE_AUX_TABLES)
        for statement in INDEXES:
            conn.execute(statement)

        # 11. user_version повышается последним шагом, после всех проверок.
        conn.execute("PRAGMA user_version=4")
    report["jobs"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    report["archive_rows"] = conn.execute("SELECT COUNT(*) FROM application_history").fetchone()[0]
    return report
