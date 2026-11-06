import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from job_finder.adapters.base import SearchHit, SearchReport
from job_finder.async_store import AsyncStore
from job_finder.models import Letter
from job_finder.store import Store
from tests.test_rules import vacancy


class AsyncStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "agent.sqlite3"
        self.store = AsyncStore(self.path)
        await self.store.start()

    async def asyncTearDown(self):
        await self.store.close()

    async def test_schema_and_bootstrap_match_sync_store(self):
        cursor = await self.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in await cursor.fetchall()}
        await cursor.close()
        for table in ("jobs", "settings", "inbox", "outbox", "skip_events",
                      "application_history", "round_requests"):
            self.assertIn(table, tables)
        pilot = await self.store.get("137924141")
        self.assertEqual(pilot["status"], "skipped")
        self.assertEqual(await self.store.setting("DELIVER"), False)
        self.assertTrue(await self.store.is_paused())

    async def test_writes_go_through_single_consumer(self):
        self.assertEqual(self.store._queue.qsize(), 0)
        await self.store.pause(False)
        self.assertFalse(await self.store.is_paused())
        await asyncio.sleep(0.01)  # очередь разобрана потребителем
        self.assertEqual(self.store._queue.qsize(), 0)

    async def test_command_search_records_round_before_offset(self):
        # Приём /search: round_requests сохраняется до продвижения offset (R5).
        recorded = await self.store.command(10, "search")
        self.assertTrue(recorded)
        cursor = await self.store._conn.execute(
            "SELECT state FROM round_requests ORDER BY id")
        states = [row[0] for row in await cursor.fetchall()]
        await cursor.close()
        self.assertEqual(states, ["queued"])
        self.assertEqual(await self.store.setting("telegram_offset"), 11)
        # Повторный /search при активном раунде не создаёт второй раунд.
        duplicate = await self.store.command(11, "search")
        self.assertTrue(duplicate)
        cursor = await self.store._conn.execute("SELECT COUNT(*) FROM round_requests")
        count = (await cursor.fetchone())[0]
        await cursor.close()
        self.assertEqual(count, 1)
        reply = [row["text"] for row in await self.store.pending_messages()]
        self.assertIn("уже выполняется", reply[-1])

    async def test_round_lifecycle_queued_active_done(self):
        await self.store.round_request()
        round_id = await self.store.round_next()
        self.assertIsNotNone(round_id)
        self.assertEqual(await self.store.round_next(), None)  # active не берётся
        report = SearchReport(hits=[SearchHit(native_id="1001"), SearchHit(native_id="1002")],
                              incomplete=False, notes=[])
        t_start = "2026-11-05T12:00:00+00:00"
        await self.store.round_commit("fake", t_start, report)
        await self.store.round_done(round_id)
        self.assertEqual((await self.store.get("fake:1001"))["status"], "found")
        self.assertEqual((await self.store.get("fake:1002"))["status"], "found")
        self.assertEqual(await self.store.setting("last_search:fake"), t_start)
        summary = await self.store.setting("search_report:fake")
        self.assertEqual(summary["ids"], ["1001", "1002"])

    async def test_round_commit_is_atomic_and_idempotent(self):
        await self.store.round_request()
        round_id = await self.store.round_next()
        report = SearchReport(hits=[SearchHit(native_id="1001")], incomplete=True,
                              notes=["ограничен"])
        await self.store.round_commit("fake", "2026-11-05T12:00:00+00:00", report)
        # Повторный коммит того же окна не дублирует строки.
        await self.store.round_commit("fake", "2026-11-05T12:00:00+00:00", report)
        cursor = await self.store._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE id='fake:1001'")
        self.assertEqual((await cursor.fetchone())[0], 1)
        await cursor.close()
        await self.store.round_done(round_id)

    async def test_round_waiting_resumes_only_after_timers(self):
        await self.store.round_request()
        round_id = await self.store.round_next()
        await self.store.set("next_retry:hh", time.time() + 60)
        await self.store.round_waiting(round_id, ["hh"])
        self.assertEqual(await self.store.round_resumable(), None)
        await self.store.set("next_retry:hh", time.time() - 1)
        resumed = await self.store.round_resumable()
        self.assertEqual(resumed, (round_id, ["hh"]))
        await self.store.round_done(round_id)

    async def test_recover_rounds_requeues_active(self):
        await self.store.round_request()
        round_id = await self.store.round_next()
        await self.store.recover_rounds()
        self.assertEqual(await self.store.round_next(), round_id)

    async def test_first_found_and_prepare_flow(self):
        await self.store.discover("fake", vacancy(id="1001"))
        self.assertEqual(await self.store.first_found(), "fake:1001")
        row = await self.store.get("fake:1001")
        self.assertEqual(row["status"], "found")
        letter = Letter("Письмо", ["python_backend"], [], [])
        await self.store.make_ready("fake:1001", letter, "подходит")
        self.assertEqual((await self.store.get("fake:1001"))["status"], "ready")

    async def test_rows_and_counts_match_sync_semantics(self):
        for job_id in ("101", "102", "103"):
            await self.store.discover("fake", vacancy(id=job_id))
            await self.store.decide("fake:" + job_id, "needs_clarification", "уточнение")
        rows = await self.store.rows(["needs_clarification"], limit=2)
        self.assertEqual([row["id"] for row in rows], ["fake:101", "fake:102"])
        self.assertEqual((await self.store.counts())["needs_clarification"], 3)

    async def test_legacy_sync_store_and_async_store_share_database(self):
        # Синхронный Store (CLI) и AsyncStore работают с одной базой.
        await self.store.enqueue("событие", kind="event")
        sync = Store(self.path)
        try:
            self.assertEqual(len(sync.pending_messages()), 0)
            sync.pause(False)
        finally:
            sync.close()
        self.assertFalse(await self.store.is_paused())
