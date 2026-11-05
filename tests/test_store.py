import tempfile
import unittest
from pathlib import Path
from job_finder.models import Letter
from job_finder.store import Store
from tests.test_rules import vacancy


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"queue.db"
        self.db=Store(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def ready(self):
        self.db.discover(vacancy())
        self.db.decide("123","ready","подходит")
        self.db.save_letter("123",Letter("Письмо",["fastapi"],[],[]))

    def test_decided_row_survives_search(self):
        self.db.discover(vacancy())
        self.db.decide("123","rejected","Python не упомянут")
        self.assertFalse(self.db.discover(vacancy()))
        self.assertEqual(self.db.get("123")["status"],"rejected")
        self.assertEqual(self.db.get("123")["reason"],"Python не упомянут")

    def test_fresh_database_has_no_attempts_table(self):
        # Этап 1b: активный код откликов и таблица attempts удалены;
        # данные старых БД переносятся в архив в этапе 1c.
        tables={row[0] for row in self.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("jobs",tables)
        self.assertIn("outbox",tables)
        self.assertNotIn("attempts",tables)

    def test_pause_is_a_delivery_pause_setting(self):
        self.assertTrue(self.db.is_paused())
        self.db.pause(False)
        self.assertFalse(self.db.is_paused())
        self.db.pause(True)
        self.assertTrue(self.db.is_paused())

    def test_generic_events_are_retained_without_proactive_delivery(self):
        self.db.enqueue("Нужно уточнение: Python разработчик")
        self.db.enqueue("Отклик подтверждён: текст общего события")
        self.assertEqual(self.db.pending_messages(),[])
        self.assertEqual([tuple(row) for row in self.db.conn.execute(
            "SELECT text,kind,sent FROM outbox ORDER BY id")], [
                ("Нужно уточнение: Python разработчик","event",0),
                ("Отклик подтверждён: текст общего события","event",0),
            ])

    def test_command_reply_is_deliverable_once(self):
        self.db.enqueue("Фоновое событие")
        self.assertTrue(self.db.command(80,"status","Ответ по запросу"))
        self.assertFalse(self.db.command(80,"status","Повтор ответа"))
        messages=self.db.pending_messages()
        self.assertEqual([(row["text"],row["kind"]) for row in messages],[("Ответ по запросу","reply")])
        self.db.delivered(messages[0]["id"])
        self.assertEqual(self.db.pending_messages(),[])

    def test_explicit_notification_kinds_filter_before_delivery_limit(self):
        for index in range(25):
            self.db.enqueue("Фоновое событие "+str(index))
        self.db.enqueue("Ответ",kind="reply")
        self.db.enqueue("Успешный отклик",kind="success")
        self.assertEqual([row["text"] for row in self.db.pending_messages()],["Ответ","Успешный отклик"])
        with self.assertRaises(ValueError):
            self.db.enqueue("Неизвестный тип",kind="urgent")

    def test_v2_outbox_migration_preserves_state_and_only_delivers_legacy_success(self):
        self.db.set("telegram_chunk_42",2)
        self.db.set("telegram_chunk_41",1)
        self.db.command(90,"status")
        snapshots={table:[tuple(row) for row in self.db.conn.execute("SELECT * FROM "+table)]
                   for table in ("jobs","settings","inbox","skip_events")}
        legacy=[
            (41,"Нужно уточнение: Python разработчик",0,"2026-10-20T01:00:00+00:00"),
            (42,"Отклик подтверждён: Python разработчик",0,"2026-10-20T02:00:00+00:00"),
            (43,"Отклик подтверждён: уже доставлен",1,"2026-10-20T03:00:00+00:00"),
            (44,"Проверка: Отклик подтверждён: ещё не успех",0,"2026-10-20T04:00:00+00:00"),
            (45,"Отклик подтверждён:без пробела",0,"2026-10-20T05:00:00+00:00"),
        ]
        with self.db.conn:
            self.db.conn.execute("DROP TABLE outbox")
            self.db.conn.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, text TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL)")
            self.db.conn.executemany("INSERT INTO outbox VALUES(?,?,?,?)",legacy)
            self.db.conn.execute("PRAGMA user_version=2")
        self.db.close()
        self.db=Store(self.path)
        self.assertEqual([row["id"] for row in self.db.pending_messages()],[42])
        self.assertEqual(self.db.conn.execute("PRAGMA user_version").fetchone()[0],3)
        self.assertEqual([tuple(row) for row in self.db.conn.execute("SELECT id,text,sent,created FROM outbox ORDER BY id")],legacy)
        for table,expected in snapshots.items():
            self.assertEqual([tuple(row) for row in self.db.conn.execute("SELECT * FROM "+table)],expected,table)
        self.db.enqueue("Отклик подтверждён: новое общее событие")
        self.db.delivered(42)
        self.db.close()
        self.db=Store(self.path)
        self.assertEqual(self.db.pending_messages(),[])
        self.assertEqual(self.db.setting("telegram_chunk_42"),2)
        self.assertEqual(self.db.setting("telegram_chunk_41"),1)
        self.assertEqual(self.db.conn.execute("SELECT MAX(id) FROM outbox").fetchone()[0],46)

    def test_future_database_version_is_rejected_without_downgrade(self):
        self.db.conn.execute("PRAGMA user_version=4")
        with self.assertRaises(ValueError):
            Store(self.path)
        self.assertEqual(self.db.conn.execute("PRAGMA user_version").fetchone()[0],4)

    def test_rows_pagination_respects_status_and_order(self):
        for job_id in ("101","102","103","104","105"):
            self.db.discover(vacancy(id=job_id))
            self.db.decide(job_id,"needs_clarification","Нужно уточнение")
        self.assertEqual([row["id"] for row in self.db.rows(["needs_clarification"],limit=2,offset=2)],["103","104"])
        self.assertEqual([row["id"] for row in self.db.rows(["needs_clarification"],limit=2,newest=True,offset=2)],["103","102"])
        self.assertEqual(self.db.rows(["needs_clarification"],offset=5),[])

    def test_update_dedupe_survives_restart(self):
        self.db.pause(False)
        self.assertTrue(self.db.command(51,"pause","Telegram только показывает отчёты"))
        self.db.close()
        self.db=Store(self.path)
        self.assertFalse(self.db.command(51,"resume","Повторный ответ"))
        self.assertFalse(self.db.is_paused())
        self.assertEqual(self.db.setting("telegram_offset"),52)
        self.assertEqual([tuple(row) for row in self.db.conn.execute("SELECT update_id,command FROM inbox")],[(51,"pause")])
        self.assertEqual([row["text"] for row in self.db.pending_messages()],["Telegram только показывает отчёты"])

    def test_confirmed_pilot_is_never_queued(self):
        self.assertEqual(self.db.get("137924141")["status"],"sent")

    def test_legacy_requeue_commands_preserve_every_job_state_and_letter(self):
        statuses=("found","rejected","needs_clarification","error","closed","ready","sending","unknown","sent")
        for number,status in enumerate(statuses,start=200):
            with self.subTest(status=status):
                job_id=str(number)
                self.db.discover(vacancy(id=job_id))
                self.db.save_letter(job_id,Letter("Сохранённое письмо",["fastapi"],[],[]))
                self.db.decide(job_id,status,"Сохранённая причина")
                original=tuple(self.db.conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone())
                self.assertTrue(self.db.command(number,"requeue:"+job_id,"Повторная оценка через Telegram недоступна"))
                self.assertEqual(tuple(self.db.conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()),original)
                self.assertEqual(self.db.setting("telegram_offset"),number+1)

    def test_legacy_control_commands_preserve_worker_settings(self):
        for number,action in enumerate(("pause","resume","search","reconcile"),start=70):
            with self.subTest(action=action):
                self.db.pause(action!="pause")
                self.db.set("browser_blocked",True)
                self.db.set("attention_reason","Нужно войти в HH")
                self.db.set("search_requested",False)
                self.db.set("reconcile_requested",False)
                original=[tuple(row) for row in self.db.conn.execute("SELECT * FROM settings WHERE key!='telegram_offset' ORDER BY key")]
                self.assertTrue(self.db.command(number,action,"Управление через Telegram недоступно"))
                self.assertEqual([tuple(row) for row in self.db.conn.execute("SELECT * FROM settings WHERE key!='telegram_offset' ORDER BY key")],original)
                self.assertEqual(self.db.setting("telegram_offset"),number+1)
                self.assertEqual(self.db.pending_messages()[-1]["kind"],"reply")
