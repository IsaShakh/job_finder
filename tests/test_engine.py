import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from hh_agent.engine import Engine
from hh_agent.models import AttentionRequired, Letter, Outcome, Salary
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
        return self.current

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

    async def test_discovery_includes_all_ids_and_preserves_pilot(self):
        await self.engine.search()
        self.assertEqual(self.db.get("123")["status"],"found")
        self.assertEqual(self.db.get("137924141")["status"],"sent")

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
