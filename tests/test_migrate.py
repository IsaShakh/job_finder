import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_finder.migrate import migrate
from job_finder.store import Store

OLD_SCHEMA = """
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '', letter TEXT, resume_id TEXT,
  evidence TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE attempts (
  id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, started TEXT NOT NULL,
  ended TEXT, status TEXT NOT NULL, evidence TEXT);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE inbox (update_id INTEGER PRIMARY KEY, command TEXT, created TEXT NOT NULL);
CREATE TABLE outbox (id INTEGER PRIMARY KEY, text TEXT NOT NULL,
  sent INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'event');
CREATE TABLE skip_events (id INTEGER PRIMARY KEY, job_id TEXT NOT NULL,
  reason TEXT NOT NULL, created TEXT NOT NULL, reported INTEGER NOT NULL DEFAULT 0);
"""

# Распределение живой БД на 05.11.2026: всего 1690 строк, включая пилот
# 137924141 среди двух sent-строк.
LIVE_DISTRIBUTION = [
    ("found", 1573), ("needs_clarification", 72), ("ready", 31),
    ("error", 8), ("sent", 1), ("rejected", 2), ("closed", 2),
]


def vacancy_data(job_id, title="Python разработчик", test=False):
    return json.dumps({
        "id": job_id, "title": title, "description": "Разработка API на Python",
        "company": "Компания", "salary": {"minimum": 200000, "maximum": None,
        "currency": "RUB", "period": "month", "raw": "от 200 000 ₽"},
        "remote": True, "test_required": test, "closed": False,
        "requirements": "FastAPI",
    }, ensure_ascii=False)


def build_legacy(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA)
    with conn:
        counter = 0
        rows = []
        for status, count in LIVE_DISTRIBUTION:
            for _ in range(count):
                counter += 1
                job_id = str(counter)
                letter = None
                evidence = ""
                reason = ""
                if status == "ready":
                    letter = json.dumps({"text": "Старое письмо", "fact_ids": ["python_backend"],
                                         "added_claims": [], "preparation": []}, ensure_ascii=False)
                    reason = "Зарплата подходит"
                if status == "sent":
                    evidence = "Подтверждены история HH и чат " + job_id
                    reason = "Контрольный отклик от 21.10.2026"
                if status == "rejected":
                    reason = "Python не упомянут"
                rows.append((job_id, vacancy_data(job_id, test=(status == "found" and counter % 100 == 0)),
                             status, reason, letter, "resume-old", evidence,
                             "2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00"))
        conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)", rows)
        # Пилот 137924141 — контрольный отклик; сверяется по содержимому.
        conn.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                     ("137924141",
                      json.dumps({"id": "137924141",
                                  "title": "Инженер поддержки и развития (LLM-агенты)",
                                  "description": "Python", "company": "Ат Проф",
                                  "remote": True, "test_required": False, "closed": False},
                                 ensure_ascii=False),
                      "sent", "Контрольный отклик от 21.10.2026", None, "resume-pilot",
                      "Подтверждены история HH и чат 5727309591",
                      "2026-10-21T00:00:00+00:00", "2026-10-21T00:00:00+00:00"))
        # attempts: одна строка с отправкой.
        conn.execute("INSERT INTO attempts(job_id,started,ended,status,evidence)"
                     " VALUES('137924141','2026-10-21T10:00:00+00:00',"
                     "'2026-10-21T10:01:00+00:00','sent','Подтверждено вручную')")
        for number in range(1, 107):
            conn.execute("INSERT INTO outbox(text,sent,created,kind) VALUES(?,?,?,?)",
                         ("Событие " + str(number), 1 if number <= 38 else 0,
                          "2026-10-20T%02d:00:00+00:00" % (number % 24),
                          "event" if number != 50 else "success"))
        for key, value in {"paused": "true", "last_search": "1",
                           "HH_LIVE": "false", "next_send_at": "2",
                           "search_requested": "false", "reconcile_requested": "false",
                           "telegram_chunk_5": "1", "digest_day": "2026-10-01",
                           "search_report": '{"ids": ["1"], "pages": 1, "incomplete": false, "notes": []}'}.items():
            conn.execute("INSERT INTO settings VALUES (?,?)", (key, value))
        conn.execute("PRAGMA user_version=3")
    conn.close()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "agent.sqlite3"

    def open_store(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        return store

    def test_migration_matches_live_distribution_without_data_loss(self):
        build_legacy(self.path)
        before = sqlite3.connect(str(self.path))
        before_count = before.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        before.close()
        store = self.open_store()

        # 1690 строк до и после; user_version 3→4.
        self.assertEqual(before_count, 1690)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1690)
        self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], 4)

        # Ключи source:native_id.
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs WHERE id NOT GLOB 'hh:*'").fetchone()[0], 0)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs WHERE source='hh' AND native_id IS NOT NULL").fetchone()[0], 1690)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs WHERE id='hh:'||native_id").fetchone()[0], 1690)

        # Карта переходов статусов.
        counts = dict(store.conn.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))
        self.assertEqual(counts, {"found": 1573, "needs_clarification": 72, "ready": 31,
                                  "error": 8, "skipped": 2, "rejected": 2, "closed": 2})
        skipped = [tuple(r) for r in store.conn.execute("SELECT reason FROM jobs WHERE status='skipped'")]
        self.assertEqual(skipped, [("отклик уже отправлен ранее (архив)",)] * 2)

        # Пилот сверяется по содержимому.
        pilot = store.get("137924141")
        self.assertEqual(pilot["vacancy"].title, "Инженер поддержки и развития (LLM-агенты)")
        self.assertEqual(pilot["vacancy"].company, "Ат Проф")
        self.assertEqual(pilot["status"], "skipped")

        # test_required перенесён из data в колонку (каждая 100-я found-строка).
        self.assertEqual(store.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE test_required=1").fetchone()[0], 15)

        # Архив: attempts (1) + sent (2) = 3 строки; таблица attempts удалена.
        archive = [tuple(r) for r in store.conn.execute("SELECT origin,job_id,status FROM application_history ORDER BY id")]
        self.assertEqual(len(archive), 3)
        self.assertEqual([row[0] for row in archive].count("attempts"), 1)
        self.assertEqual([row[0] for row in archive].count("jobs"), 2)
        self.assertIn(("attempts", "137924141", "sent"), archive)
        self.assertIn(("jobs", "137924141", "sent"), archive)
        tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("attempts", tables)
        self.assertIn("application_history", tables)
        jobs_columns = {row[1] for row in store.conn.execute("PRAGMA table_info(jobs)")}
        self.assertNotIn("resume_id", jobs_columns)
        self.assertNotIn("evidence", jobs_columns)
        self.assertIn("native_id", jobs_columns)

        # ready: 31 строка со снимком legacy_letter; старые письма не используются.
        ready = list(store.conn.execute("SELECT data,rules_version FROM jobs WHERE status='ready'"))
        self.assertEqual(len(ready), 31)
        for data, rules_version in ready:
            self.assertEqual(rules_version, "legacy")
            self.assertEqual(json.loads(data).get("legacy_letter"), {"text": "Старое письмо",
                "fact_ids": ["python_backend"], "added_claims": [], "preparation": []})
        for status in ("rejected", "needs_clarification"):
            leftover = store.conn.execute("SELECT COUNT(*) FROM jobs WHERE status=? AND rules_version!='legacy'",
                                          (status,)).fetchone()[0]
            self.assertEqual(leftover, 0)

        # outbox: строки сохранены, legacy=1; настроек отправки нет.
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 106)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM outbox WHERE legacy=1").fetchone()[0], 106)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM outbox WHERE sent=0").fetchone()[0], 68)
        self.assertIsNone(store.setting("HH_LIVE"))
        self.assertIsNone(store.setting("next_send_at"))
        self.assertIsNone(store.setting("search_requested"))
        self.assertIsNone(store.setting("reconcile_requested"))
        self.assertIsNone(store.setting("telegram_chunk_5"))
        self.assertEqual(store.setting("DELIVER"), True)
        self.assertEqual(store.setting("NOTIFY_MODE"), "immediate")
        self.assertEqual(store.setting("rules_version"), "v3")
        self.assertEqual(store.setting("paused"), True)

        # Бэкап создан ПЕРЕД изменениями.
        backups = list((self.path.parent / "backups").glob("agent-*.sqlite3"))
        self.assertEqual(len(backups), 1)

        # Повторное открытие — без изменений (идемпотентность).
        store.close()
        store = Store(self.path)
        self.addCleanup(store.close)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1690)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM application_history").fetchone()[0], 3)
        self.assertEqual(len(list((self.path.parent / "backups").glob("agent-*.sqlite3"))), 1)

    def test_unknown_and_sending_move_to_archive_and_clarification(self):
        build_legacy(self.path)
        conn = sqlite3.connect(str(self.path))
        with conn:
            conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                         ("900001", vacancy_data("900001"), "unknown",
                          "Результат отправки неизвестен", None, "r", "ev",
                          "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))
            conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                         ("900002", vacancy_data("900002"), "sending",
                          "Отправляется", None, "r", "",
                          "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))
        conn.close()
        store = self.open_store()
        self.assertEqual(store.get("900001")["status"], "needs_clarification")
        self.assertIn("проверьте историю hh", store.get("900001")["reason"])
        self.assertEqual(store.get("900002")["status"], "needs_clarification")
        self.assertIn("прервана", store.get("900002")["reason"])
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1692)
        # Архив: attempts (1) + sent (2) + unknown (1) + sending (1) = 5.
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM application_history").fetchone()[0], 5)

    def test_fresh_database_is_created_in_target_schema(self):
        store = self.open_store()
        tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("jobs", "settings", "inbox", "outbox", "skip_events",
                      "application_history", "clusters", "job_embeddings", "llm_calls"):
            self.assertIn(table, tables)
        self.assertNotIn("attempts", tables)
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(jobs)")}
        self.assertNotIn("resume_id", columns)
        self.assertNotIn("evidence", columns)
        self.assertIn("native_id", columns)
        self.assertIn("manual_decision", columns)
        self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], 4)
        pilot = store.get("137924141")
        self.assertEqual(pilot["status"], "skipped")
        self.assertEqual(pilot["reason"], "отклик уже отправлен ранее (архив)")
        self.assertTrue(store.is_paused())
        self.assertEqual(store.setting("DELIVER"), True)

    def test_migrate_function_is_idempotent(self):
        build_legacy(self.path)
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        report1 = migrate(conn)
        report2 = migrate(conn)
        conn.close()
        self.assertEqual(report1["jobs"], 1690)
        self.assertEqual(report1["archived_attempts"], 1)
        self.assertEqual(report1["archived_jobs"], 2)
        self.assertEqual(report1["archive_rows"], 3)
        self.assertEqual(report2["archived_attempts"], 0)
        self.assertEqual(report2["archived_jobs"], 0)
        self.assertEqual(report2["archive_rows"], 3)
        self.assertEqual(report2["jobs"], 1690)
