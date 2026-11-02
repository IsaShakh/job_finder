import tempfile
import asyncio
import time
from dataclasses import replace
import unittest
from pathlib import Path
from types import SimpleNamespace
from hh_agent.engine import Engine
from hh_agent.models import AttentionRequired, Letter, Outcome, Salary, VacancyUnavailable
from hh_agent.store import Store
from tests.test_rules import vacancy


class Site:
    def __init__(self):
        self.current=vacancy(salary=Salary(200000))
        self.submits=0
        self.result=Outcome("sent","HH: Вы откликнулись")
        self.read_error=None
        self.pause_before_gate=None

    async def search(self, max_pages):
        return {"ids":["123","137924141"],"pages":1,"incomplete":False,"notes":[]}

    async def read(self, job_id):
        if self.read_error:
            raise self.read_error
        return replace(self.current,id=job_id)

    async def apply(self, job, letter, gate):
        if self.pause_before_gate:
            self.pause_before_gate()
        if not await gate():
            return Outcome("ready",reason="пауза")
        self.submits+=1
        if isinstance(self.result,Exception):
            raise self.result
        return self.result

    async def reconcile(self, job_id):
        return self.result


class Writer:
    async def generate(self, job):
        return Letter("Точное письмо",["python_backend"],["Знаком с Kafka"],["Kafka: пример очереди"])


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Store(Path(self.temp.name)/"queue.db")
        self.site=Site()
        self.config=SimpleNamespace(live=True,resume_id="resume",max_search_pages=5,send_interval=90,search_interval=21600)
        self.engine=Engine(self.config,self.db,self.site,Writer())

    async def asyncTearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def ready(self):
        self.db.discover(self.site.current)
        await self.engine.prepare("123")
        self.db.pause(False)

    async def test_local_start_searches_immediately_and_clears_legacy_manual_pause(self):
        self.db.pause(True)
        self.db.set('last_search',time.time())
        self.db.set('browser_blocked',True)
        self.db.set('attention_reason','Старый вход')
        self.db.set('search_requested',True)
        self.db.set('reconcile_requested',True)
        await self.engine.start()
        self.assertIsNotNone(self.db.get('123'))
        self.assertFalse(self.db.is_paused())
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

    async def test_local_start_preview_prepares_without_submitting(self):
        self.config.live=False
        await self.engine.start()
        await self.engine.tick()
        await self.engine.tick()
        self.assertEqual(self.db.get('123')['status'],'ready')
        self.assertEqual(self.site.submits,0)

    async def test_local_start_live_continues_work_without_telegram_resume(self):
        await self.engine.start()
        await self.engine.tick()
        await self.engine.tick()
        self.assertEqual(self.db.get('123')['status'],'sent')
        self.assertEqual(self.site.submits,1)

    async def test_start_reconciles_interrupted_send_before_continuing(self):
        await self.ready()
        self.db.claim('123','resume')
        await self.engine.start()
        self.assertEqual(self.db.get('123')['status'],'sent')
        self.assertFalse(self.db.is_paused())
        self.assertEqual(self.site.submits,0)
        self.assertIsNotNone(self.db.setting('search_report'))

    async def test_start_with_unknown_result_searches_but_keeps_submissions_blocked(self):
        await self.ready()
        self.db.claim('123','resume')
        self.site.result=Outcome('unknown',reason='Нет подтверждения')
        await self.engine.start()
        self.assertEqual(self.db.get('123')['status'],'unknown')
        self.assertTrue(self.db.is_paused())
        self.assertIsNotNone(self.db.setting('search_report'))
        self.assertEqual(self.site.submits,0)

    async def test_start_cannot_continue_after_reconciliation_requires_login(self):
        await self.ready()
        self.db.claim('123','resume')
        async def expired_login(job_id):
            raise AttentionRequired('Нужен вход')
        self.site.reconcile=expired_login
        await self.engine.start()
        self.assertTrue(self.db.is_paused())
        self.assertTrue(self.db.setting('browser_blocked'))
        self.assertIsNone(self.db.setting('search_report'))
        self.assertEqual(self.site.submits,0)

    async def test_search_continues_while_submissions_are_guarded(self):
        self.db.pause(True)
        await self.engine.tick()
        self.assertIsNotNone(self.db.get('123'))
        self.assertTrue(self.db.is_paused())
        self.assertEqual(self.site.submits,0)

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
        self.assertEqual(self.db.get("137924141")["status"],"sent")
        self.assertEqual(self.db.pending_messages(),[])

    async def test_preparation_clarification_is_saved_without_notification(self):
        self.site.current=replace(self.site.current,remote=None)
        self.db.discover(self.site.current)
        await self.engine.prepare('123')
        self.assertEqual(self.db.get('123')['status'],'needs_clarification')
        self.assertTrue(self.db.get('123')['reason'])
        self.assertEqual(self.db.pending_messages(),[])

    async def test_changed_conditions_clarification_is_saved_without_notification(self):
        await self.ready()
        self.site.current=replace(self.site.current,remote=None)
        await self.engine.send('123')
        self.assertEqual(self.db.get('123')['status'],'needs_clarification')
        self.assertEqual(self.site.submits,0)
        self.assertEqual(self.db.pending_messages(),[])

    async def test_form_questions_are_saved_without_notification(self):
        await self.ready()
        async def questionnaire(job, letter, gate):
            return Outcome('needs_clarification',reason='В анкете есть дополнительные вопросы работодателя')
        self.site.apply=questionnaire
        await self.engine.send('123')
        self.assertEqual(self.db.get('123')['status'],'needs_clarification')
        self.assertIn('вопросы',self.db.get('123')['reason'])
        self.assertEqual(self.site.submits,0)
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
        self.assertEqual(self.site.submits,0)

    async def test_unavailable_ready_job_is_not_sent_and_next_ready_job_continues(self):
        await self.ready()
        self.db.discover(replace(self.site.current,id='456'))
        await self.engine.prepare('456')
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
        self.assertEqual(self.site.submits,0)
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],0)
        self.assertEqual(self.db.pending_messages(),[])
        await self.engine.tick()
        self.assertEqual(self.db.get('456')['status'],'sent')
        self.assertEqual(self.site.submits,1)

    async def test_unavailable_after_claim_keeps_unknown_and_blocks_repeat(self):
        await self.ready()
        self.site.result=VacancyUnavailable('Вам недоступна эта вакансия')
        await self.engine.send('123')
        await self.engine.send('123')
        self.assertEqual(self.db.get('123')['status'],'unknown')
        self.assertTrue(self.db.is_paused())
        self.assertEqual(self.site.submits,1)
        self.assertEqual([row[0] for row in self.db.conn.execute('SELECT status FROM attempts')],['unknown'])
        self.assertEqual(self.db.pending_messages(),[])

    async def test_changed_salary_blocks_prepared_application(self):
        await self.ready()
        self.site.current=vacancy(salary=Salary(100000,120000))
        await self.engine.send("123")
        self.assertEqual(self.site.submits,0)
        self.assertEqual(self.db.get("123")["status"],"rejected")

    async def test_unknown_is_paused_and_never_retried(self):
        await self.ready()
        self.site.result=TimeoutError()
        await self.engine.send("123")
        await self.engine.send("123")
        self.assertEqual(self.site.submits,1)
        self.assertEqual(self.db.get("123")["status"],"unknown")
        self.assertTrue(self.db.is_paused())
        self.assertEqual(self.db.pending_messages(),[])

    async def test_success_notification_contains_exact_letter_and_private_claims(self):
        await self.ready()
        await self.engine.send("123")
        self.assertEqual(self.db.get("123")["status"],"sent")
        notification="\n".join(x["text"] for x in self.db.pending_messages())
        self.assertIn("Точное письмо",notification)
        self.assertIn("Знаком с Kafka",notification)
        self.assertIn("Kafka: пример очереди",notification)

    async def test_expired_auth_pauses_without_unknown_send(self):
        await self.ready()
        self.site.read_error=AttentionRequired("Нужен вход")
        await self.engine.send("123")
        self.assertTrue(self.db.is_paused())
        self.assertEqual(self.db.get("123")["status"],"ready")
        self.assertEqual(self.site.submits,0)
        self.assertEqual(self.db.pending_messages(),[])
        self.assertEqual(self.db.setting('attention_reason'),'Нужен вход')

    async def test_preview_cannot_submit_even_after_resume(self):
        await self.ready()
        self.config.live=False
        await self.engine.send("123")
        self.assertEqual(self.site.submits,0)

    async def test_pause_between_form_fill_and_click(self):
        await self.ready()
        self.site.pause_before_gate=lambda:self.db.pause(True)
        await self.engine.send("123")
        self.assertEqual(self.site.submits,0)
        self.assertEqual(self.db.get("123")["status"],"ready")

    async def test_test_skip_persists_and_appears_in_digest(self):
        self.site.current=vacancy(salary=Salary(200000),test_required=True)
        self.db.discover(self.site.current)
        await self.engine.prepare("123")
        await self.engine.search()
        self.assertEqual(self.db.get("123")["status"],"rejected")
        self.assertIn("предложено пройти тест",self.engine.digest())

    async def test_reconcile_confirms_without_sending_again(self):
        await self.ready()
        self.db.claim("123","resume")
        self.db.recover()
        await self.engine.reconcile()
        self.assertEqual(self.db.get("123")["status"],"sent")
        self.assertEqual(self.site.submits,0)

    async def test_interrupted_letter_does_not_block_a_later_ready_job(self):
        class InterruptedWriter:
            async def generate(self, job):
                raise asyncio.CancelledError()
        self.db.discover(self.site.current)
        self.engine.writer=InterruptedWriter()
        with self.assertRaises(asyncio.CancelledError):
            await self.engine.prepare('123')
        self.db.recover()
        self.assertEqual(self.db.get('123')['status'],'found')
        self.engine.writer=Writer()
        self.db.discover(vacancy(id='456',salary=Salary(200000)))
        await self.engine.prepare('456')
        self.db.pause(False)
        self.db.set('last_search',time.time())
        await self.engine.tick()
        self.assertEqual(self.db.get('456')['status'],'sent')

    async def test_legacy_ready_without_letter_is_repaired_on_restart(self):
        self.db.discover(self.site.current)
        self.db.decide('123','ready','подходит')
        self.db.recover()
        self.assertEqual(self.db.get('123')['status'],'found')

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
        self.db.decide('123','rejected','Пропущена: предложено пройти тест')
        # Day change after the first report; the event is now from yesterday.
        with self.db.conn:
            self.db.conn.execute("UPDATE jobs SET updated='2000-01-01T12:00:00+00:00' WHERE id='123'")
        self.db.set('digest_day','2000-01-01')
        await self.engine.tick()
        self.assertEqual(self.db.pending_messages(),[])
        self.assertEqual(self.db.get('123')['status'],'rejected')
        self.assertIn('предложено пройти тест',self.db.get('123')['reason'])
