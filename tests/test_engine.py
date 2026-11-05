import tempfile
import asyncio
import time
from dataclasses import replace
import unittest
from pathlib import Path
from types import SimpleNamespace
from job_finder.engine import Engine, TEST_LABEL
from job_finder.models import AttentionRequired, Letter, Salary, VacancyUnavailable
from job_finder.store import Store
from tests.test_rules import vacancy


class Site:
    def __init__(self):
        self.current=vacancy(salary=Salary(200000))
        self.read_error=None

    async def search(self, max_pages):
        return {"ids":["123","137924141"],"pages":1,"incomplete":False,"notes":[]}

    async def read(self, job_id):
        if self.read_error:
            raise self.read_error
        return replace(self.current,id=job_id)


class Writer:
    async def generate(self, job):
        return Letter("Точное письмо",["python_backend"],["Знаком с Kafka"],["Kafka: пример очереди"])


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Store(Path(self.temp.name)/"queue.db")
        self.site=Site()
        self.config=SimpleNamespace(max_search_pages=5,search_interval=21600)
        self.engine=Engine(self.config,self.db,self.site,Writer())

    async def asyncTearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def test_local_start_searches_immediately_and_clears_legacy_flags(self):
        self.db.pause(True)
        self.db.set('last_search',time.time())
        self.db.set('browser_blocked',True)
        self.db.set('attention_reason','Старый вход')
        self.db.set('search_requested',True)
        self.db.set('reconcile_requested',True)
        await self.engine.start()
        self.assertIsNotNone(self.db.get('123'))
        # Пауза — это пауза доставки; запуск её не трогает.
        self.assertTrue(self.db.is_paused())
        self.assertFalse(self.db.setting('browser_blocked'))
        self.assertFalse(self.db.setting('search_requested'))
        self.assertFalse(self.db.setting('reconcile_requested'))
        self.assertEqual(self.db.pending_messages(),[])

    async def test_run_calls_start_without_waiting_for_a_telegram_command(self):
        self.db.set('last_search',time.time())
        searched=asyncio.Event()
        original_search=self.site.search
        async def search(max_pages):
            report=await original_search(max_pages)
            searched.set()
            return report
        self.site.search=search
        task=asyncio.create_task(self.engine.run())
        try:
            await asyncio.wait_for(searched.wait(),timeout=0.2)
            self.assertIsNotNone(self.db.get('123'))
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_local_start_prepares_found_jobs_without_submission(self):
        await self.engine.start()
        await self.engine.tick()
        await self.engine.tick()
        self.assertEqual(self.db.get('123')['status'],'ready')
        self.assertIsNotNone(self.db.get('123')['letter'])

    async def test_search_runs_while_delivery_is_paused(self):
        self.db.pause(True)
        await self.engine.tick()
        self.assertIsNotNone(self.db.get('123'))
        self.assertTrue(self.db.is_paused())

    async def test_legacy_request_flags_cannot_clear_an_access_block(self):
        self.db.set('browser_blocked',True)
        self.db.set('search_requested',True)
        self.db.set('reconcile_requested',True)
        await self.engine.tick()
        self.assertTrue(self.db.setting('browser_blocked'))
        self.assertIsNone(self.db.get('123'))

    async def test_discovery_includes_all_ids_and_preserves_pilot(self):
        await self.engine.search()
        self.assertEqual(self.db.get("123")["status"],"found")
        self.assertEqual(self.db.get("137924141")["status"],"skipped")
        self.assertEqual(self.db.pending_messages(),[])

    async def test_preparation_clarification_is_saved_without_notification(self):
        self.site.current=replace(self.site.current,remote=None)
        self.db.discover(self.site.current)
        await self.engine.prepare('123')
        self.assertEqual(self.db.get('123')['status'],'needs_clarification')
        self.assertTrue(self.db.get('123')['reason'])
        self.assertEqual(self.db.pending_messages(),[])

    async def test_preparation_error_is_saved_without_notification(self):
        self.db.discover(self.site.current)
        self.site.read_error=RuntimeError('read failed')
        await self.engine.prepare('123')
        self.assertEqual(self.db.get('123')['status'],'error')
        self.assertEqual(self.db.pending_messages(),[])

    async def test_unavailable_vacancy_requires_clarification_and_next_job_is_prepared(self):
        self.db.discover(self.site.current)
        self.db.discover(replace(self.site.current,id='456'))
        self.db.pause(False)
        self.db.set('last_search',time.time())
        original_read=self.site.read
        async def read(job_id):
            if job_id=='123':
                raise VacancyUnavailable('Вам недоступна эта вакансия')
            return await original_read(job_id)
        self.site.read=read
        await self.engine.tick()
        self.assertEqual(self.db.get('123')['status'],'needs_clarification')
        self.assertEqual(self.db.get('123')['reason'],'Вам недоступна эта вакансия')
        self.assertFalse(self.db.is_paused())
        self.assertFalse(self.db.setting('browser_blocked',False))
        self.assertEqual(self.db.pending_messages(),[])
        await self.engine.tick()
        self.assertEqual(self.db.get('456')['status'],'ready')

    async def test_test_required_is_a_label_not_a_rejection(self):
        self.site.current=vacancy(salary=Salary(200000),test_required=True)
        self.db.discover(self.site.current)
        await self.engine.prepare('123')
        self.assertEqual(self.db.get('123')['status'],'ready')
        self.assertIn(TEST_LABEL,self.db.get('123')['reason'])

    async def test_test_label_added_to_clarification_reason(self):
        self.site.current=vacancy(salary=Salary(200000),test_required=True,remote=None)
        self.db.discover(self.site.current)
        await self.engine.prepare('123')
        self.assertEqual(self.db.get('123')['status'],'needs_clarification')
        self.assertIn(TEST_LABEL,self.db.get('123')['reason'])

    async def test_closed_job_does_not_block_next_job(self):
        self.db.discover(self.site.current)
        self.db.discover(vacancy(id='456',salary=Salary(200000)))
        self.site.current=replace(self.site.current,closed=True)
        await self.engine.prepare('123')
        self.assertEqual(self.db.get('123')['status'],'closed')
        self.site.current=replace(self.site.current,closed=False)
        await self.engine.prepare('456')
        self.assertEqual(self.db.get('456')['status'],'ready')

    async def test_daily_digests_do_not_send_unsolicited_notifications(self):
        self.db.set('digest_day','2000-01-01')
        self.db.set('last_search',time.time())
        await self.engine.tick()
        self.db.discover(self.site.current)
        self.db.decide('123','rejected','Python не упомянут')
        # Day change after the first report; the event is now from yesterday.
        with self.db.conn:
            self.db.conn.execute("UPDATE jobs SET updated='2000-01-01T12:00:00+00:00' WHERE id='123'")
        self.db.set('digest_day','2000-01-01')
        await self.engine.tick()
        self.assertEqual(self.db.pending_messages(),[])
        self.assertEqual(self.db.get('123')['status'],'rejected')
        self.assertEqual(self.db.get('123')['reason'],'Python не упомянут')

    async def test_attention_pauses_delivery_and_blocks_work(self):
        self.engine.attention('Нужен вход')
        self.assertTrue(self.db.is_paused())
        self.assertTrue(self.db.setting('browser_blocked'))
        self.assertEqual(self.db.setting('attention_reason'),'Нужен вход')
        self.assertIsNone(self.db.setting('search_report'))
