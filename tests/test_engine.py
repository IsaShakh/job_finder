import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_finder.adapters.base import SearchHit, SearchReport
from job_finder.async_store import AsyncStore
from job_finder.config import Config
from job_finder.dedupe import content_hash as dedupe_content_hash
from job_finder.engine import Engine, TEST_LABEL
from job_finder.models import (AttentionRequired, Letter, RateLimited, Salary,
                               Vacancy, WriterResult)
from tests.test_rules import vacancy


class Writer:
    def __init__(self):
        self.calls = 0

    async def generate(self, job):
        self.calls += 1
        return WriterResult(score=80, score_reason="опыт совпадает", score_status="ok",
                            letter=Letter("Точное письмо", ["python_backend"], [], []),
                            letter_status="ok", advice=[], advice_status="ok",
                            usage={"model": "gpt-4o-mini", "prompt_tokens": 10,
                                   "completion_tokens": 20})


class Probe:
    """Программируемый источник для тестов раунда."""
    def __init__(self, name, hits=(), failures=0, fetch_for=None):
        self.name = name
        self.hits = hits
        self.failures = failures
        self.calls = 0
        self.fetch_for = fetch_for or {}
        self.closed = False

    async def search(self, criteria, since):
        self.calls += 1
        if self.calls <= self.failures:
            raise RateLimited("лимит")
        return SearchReport(hits=[SearchHit(native_id=str(item)) for item in self.hits],
                            incomplete=False, notes=[])

    async def fetch(self, native_id):
        if native_id in self.fetch_for:
            return self.fetch_for[native_id]
        return vacancy(id=native_id, salary=Salary(200000))

    async def close(self):
        self.closed = True


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(__import__("os").environ, {}, clear=True):
            self.config = Config.load(Path(self.temp.name))
        self.store = AsyncStore(self.config.db_path)
        await self.store.start()
        self.addAsyncCleanup(self.store.close)

    def engine(self, adapters, writer=None):
        return Engine(self.config, self.store, adapters, writer or Writer())

    async def test_start_requeues_interrupted_active_round(self):
        await self.store.round_request()
        round_id = await self.store.round_next()
        source = Probe("probe", hits=(1001,))
        engine = self.engine([source])
        await engine.start()
        self.assertEqual(await self.store.round_next(), round_id)

    async def test_round_polls_sources_in_parallel_and_commits_atomically(self):
        # Если опрос был бы последовательным, первый источник завис бы
        # на ожидании второго и тест упал по таймауту.
        gate = SimpleNamespace(started=0, second=asyncio.Event())
        class GateProbe(Probe):
            async def search(self, criteria, since):
                gate.started += 1
                if gate.started == 1:
                    await asyncio.wait_for(gate.second.wait(), timeout=1)
                else:
                    gate.second.set()
                return await super().search(criteria, since)
        first = GateProbe("first", hits=(1001, 1002))
        second = GateProbe("second", hits=(2001,))
        await self.store.round_request()
        engine = self.engine([first, second])
        await engine.run_pending_rounds()
        self.assertEqual(gate.started, 2)
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)
        self.assertEqual((await self.store.get("first:1001"))["status"], "found")
        self.assertEqual((await self.store.get("second:2001"))["status"], "found")
        self.assertIsNotNone(await self.store.setting("last_search:first"))
        self.assertIsNotNone(await self.store.setting("search_report:second"))
        cursor = await self.store._conn.execute("SELECT state FROM round_requests")
        states = [row[0] for row in await cursor.fetchall()]
        await cursor.close()
        self.assertEqual(states, ["done"])
        # Сводка раунда — обычное событие, не доставляется проактивно.
        self.assertEqual(await self.store.pending_messages(), [])

    async def test_round_isolates_rate_limited_source_and_resumes(self):
        source = Probe("probe", hits=(1001,), failures=1)
        await self.store.round_request()
        engine = self.engine([source])
        await engine.run_pending_rounds()
        # Раунд в waiting, строк нет, таймер повтора установлен.
        self.assertEqual(await self.store.round_resumable(), None)
        self.assertIsNone(await self.store.get("probe:1001"))
        self.assertGreater(await self.store.setting("next_retry:probe"), time.time())
        # Таймер прошёл — раунд возобновляется и доигрывается.
        await self.store.set("next_retry:probe", time.time() - 1)
        self.assertTrue(await engine.resume_waiting())
        self.assertEqual((await self.store.get("probe:1001"))["status"], "found")

    async def test_round_isolates_crashed_source(self):
        class Crash:
            name = "crash"
            async def search(self, criteria, since):
                raise RuntimeError("boom")
            async def fetch(self, native_id):
                return vacancy(id=native_id)
            async def close(self):
                pass
        await self.store.round_request()
        engine = self.engine([Crash()])
        await engine.run_pending_rounds()
        self.assertGreater(await self.store.setting("next_retry:crash"), time.time())
        cursor = await self.store._conn.execute("SELECT text FROM outbox ORDER BY id")
        events = [row["text"] for row in await cursor.fetchall()]
        await cursor.close()
        self.assertTrue(any("crash" in text for text in events))

    async def test_prepare_ready_with_letter_and_test_label(self):
        job = vacancy(id="1001", salary=Salary(200000), test_required=True)
        source = Probe("probe", fetch_for={"1001": job})
        await self.store.discover("probe", vacancy(id="1001"))
        engine = self.engine([source])
        await engine.prepare("probe:1001")
        row = await self.store.get("probe:1001")
        self.assertEqual(row["status"], "ready")
        self.assertIsNotNone(row["letter"])
        self.assertIn(TEST_LABEL, row["reason"])

    async def test_prepare_clarification_and_rejection(self):
        unclear = vacancy(id="1001", remote=None)
        not_python = vacancy(id="1002", title="Java developer",
                             description="Java", salary=Salary(200000))
        source = Probe("probe", fetch_for={"1001": unclear, "1002": not_python})
        await self.store.discover("probe", vacancy(id="1001"))
        await self.store.discover("probe", vacancy(id="1002"))
        engine = self.engine([source])
        await engine.prepare("probe:1001")
        await engine.prepare("probe:1002")
        self.assertEqual((await self.store.get("probe:1001"))["status"], "needs_clarification")
        self.assertEqual((await self.store.get("probe:1002"))["status"], "rejected")

    async def test_prepare_error_is_saved_without_notification(self):
        class Crash:
            name = "crash"
            async def search(self, criteria, since):
                return SearchReport(hits=[], incomplete=False, notes=[])
            async def fetch(self, native_id):
                raise RuntimeError("read failed")
            async def close(self):
                pass
        await self.store.discover("crash", vacancy(id="1001"))
        engine = self.engine([Crash()])
        await engine.prepare("crash:1001")
        self.assertEqual((await self.store.get("crash:1001"))["status"], "error")
        self.assertEqual(await self.store.pending_messages(), [])

    async def test_prepare_unknown_source_is_error(self):
        await self.store.discover("hh", vacancy(id="1001"))
        engine = self.engine([])
        await engine.prepare("hh:1001")
        row = await self.store.get("hh:1001")
        self.assertEqual(row["status"], "error")
        self.assertIn("Неизвестный источник", row["reason"])

    async def test_daily_digest_does_not_send_unsolicited_notifications(self):
        source = Probe("probe", hits=())
        engine = self.engine([source])
        await engine.tick()
        self.assertIsNotNone(await self.store.setting("digest_day"))
        self.assertIsNotNone(await self.store.setting("last_digest_at"))
        self.assertEqual(await self.store.pending_messages(), [])

    async def test_stale_legacy_row_is_reassessed_with_new_rules(self):
        # Строка, решённая старыми правилами (например, после миграции 1c),
        # переоценивается в первом раунде и получает новое письмо.
        job = vacancy(id="1001", salary=Salary(200000))
        source = Probe("probe", fetch_for={"1001": job})
        await self.store.discover("probe", vacancy(id="1001"))
        await self.store.make_ready("probe:1001",
                                    Letter("Старое письмо", ["python_backend"], [], []),
                                    "старое правило")
        await self.store._conn.execute("UPDATE jobs SET rules_version='legacy' WHERE id='probe:1001'")
        await self.store._conn.commit()
        engine = self.engine([source])
        await engine.tick()
        row = await self.store.get("probe:1001")
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["rules_version"], "v3")
        self.assertNotEqual(row["letter"].text, "Старое письмо")

    async def test_legacy_test_rejection_is_reassessed_to_ready_with_label(self):
        job = vacancy(id="1002", salary=Salary(200000), test_required=True)
        source = Probe("probe", fetch_for={"1002": job})
        await self.store.discover("probe", vacancy(id="1002"))
        await self.store.decide("probe:1002", "rejected", "Пропущена: предложено пройти тест",
                                rules_version="legacy")
        engine = self.engine([source])
        await engine.tick()
        row = await self.store.get("probe:1002")
        self.assertEqual(row["status"], "ready")
        self.assertIn(TEST_LABEL, row["reason"])

    async def test_legacy_ready_row_failing_new_rules_is_rejected(self):
        not_python = vacancy(id="1003", title="Java developer", description="Java",
                             salary=Salary(200000))
        source = Probe("probe", fetch_for={"1003": not_python})
        await self.store.discover("probe", vacancy(id="1003"))
        await self.store.make_ready("probe:1003",
                                    Letter("Старое письмо", ["python_backend"], [], []),
                                    "старое правило")
        await self.store._conn.execute("UPDATE jobs SET rules_version='legacy' WHERE id='probe:1003'")
        await self.store._conn.commit()
        engine = self.engine([source])
        await engine.tick()
        row = await self.store.get("probe:1003")
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["rules_version"], "v3")
        self.assertIsNone(row["letter"])

    async def test_card_stores_score_advice_and_logs_llm_cost(self):
        writer = Writer()
        job = vacancy(id="1001", salary=Salary(200000))
        source = Probe("probe", fetch_for={"1001": job})
        await self.store.discover("probe", vacancy(id="1001"))
        engine = self.engine([source], writer=writer)
        await engine.prepare("probe:1001")
        row = await self.store.get("probe:1001")
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["llm"]["score"], 80)
        self.assertEqual(row["llm"]["score_status"], "ok")
        self.assertEqual(row["llm"]["stamp"]["rules_version"], "v3")
        cursor = await self.store._conn.execute("SELECT kind,model,prompt_tokens,cost FROM llm_calls")
        calls = [tuple(item) for item in await cursor.fetchall()]
        await cursor.close()
        self.assertEqual(calls, [("card", "gpt-4o-mini", 10, 1.3e-05)])

    async def test_card_cache_skips_llm_when_versions_unchanged(self):
        writer = Writer()
        job = vacancy(id="1001", salary=Salary(200000))
        source = Probe("probe", fetch_for={"1001": job})
        await self.store.discover("probe", vacancy(id="1001"))
        engine = self.engine([source], writer=writer)
        await engine.prepare("probe:1001")
        first_letter = (await self.store.get("probe:1001"))["letter"].text
        # Повторная подготовка с теми же версиями — без вызова LLM (R7).
        await engine._card("probe:1001", job, dedupe_content_hash(job), "v3")
        self.assertEqual(writer.calls, 1)
        self.assertEqual((await self.store.get("probe:1001"))["letter"].text, first_letter)

    async def test_ready_job_gets_card_notification_and_deliver_flag_gates_it(self):
        job = vacancy(id="1001", salary=Salary(200000))
        source = Probe("probe", fetch_for={"1001": job})
        await self.store.discover("probe", vacancy(id="1001"))
        engine = self.engine([source])
        await engine.prepare("probe:1001")
        await engine.tick()
        cursor = await self.store._conn.execute(
            "SELECT text,kind,job_id FROM outbox WHERE kind='card'")
        cards = [tuple(item) for item in await cursor.fetchall()]
        await cursor.close()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0][2], "probe:1001")
        self.assertIn("Нашлась новая релевантная вакансия", cards[0][0])
        self.assertIn("Python разработчик", cards[0][0])
        # DELIVER=false → уведомления копятся в ready.
        await self.store.set("DELIVER", False)
        job2 = vacancy(id="1002", salary=Salary(200000))
        source.fetch_for["1002"] = job2
        await self.store.discover("probe", vacancy(id="1002"))
        await engine.prepare("probe:1002")
        await engine.tick()
        cursor = await self.store._conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='card'")
        self.assertEqual((await cursor.fetchone())[0], 1)
        await cursor.close()
        self.assertEqual((await self.store.get("probe:1002"))["status"], "ready")

    async def test_card_notification_is_unique_per_cluster(self):
        await self.store.discover("probe", vacancy(id="1001", title="Python", company="Акме"))
        await self.store.make_ready("probe:1001",
                                    Letter("Письмо", ["python_backend"], [], []), "подходит")
        await self.store.discover("probe", vacancy(id="1002", title="Python", company="Акме"))
        await self.store.make_ready("probe:1002",
                                    Letter("Письмо", ["python_backend"], [], []), "подходит")
        await self.store.join_cluster("probe:1002", ["probe:1001"])
        engine = self.engine([])
        await engine.notify_ready("probe:1001")
        await engine.notify_ready("probe:1002")
        cursor = await self.store._conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='card'")
        self.assertEqual((await cursor.fetchone())[0], 1)
        await cursor.close()

    async def test_keep_generates_letter_via_notification(self):
        job = vacancy(id="1001", salary=Salary(200000))
        source = Probe("probe", fetch_for={"1001": job})
        await self.store.discover("probe", vacancy(id="1001"))
        await self.store.decide("probe:1001", "needs_clarification", "не подтверждён формат",
                                rules_version="v3")
        engine = self.engine([source])
        self.assertTrue(await self.store.keep_job("probe:1001"))
        await engine.tick()
        row = await self.store.get("probe:1001")
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["manual_decision"], "keep")
        cursor = await self.store._conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='card'")
        self.assertEqual((await cursor.fetchone())[0], 1)
        await cursor.close()
        self.assertIsNotNone((await self.store.get("probe:1001"))["letter"])

    async def test_attention_pauses_delivery_and_blocks_work(self):
        engine = self.engine([])
        await engine.attention("Нужен вход")
        self.assertTrue(await self.store.is_paused())
        self.assertTrue(await self.store.setting("browser_blocked"))
        self.assertEqual(await self.store.setting("attention_reason"), "Нужен вход")

    async def test_start_resumes_delivery_pause(self):
        await self.store.pause(True)
        engine = self.engine([])
        await engine.start()
        self.assertFalse(await self.store.is_paused())

    async def test_disabled_source_rows_are_left_untouched(self):
        # hh не включён в SOURCES: его старые строки не готовятся и не
        # переоцениваются, пока источник не вернётся.
        await self.store.discover("hh", vacancy(id="900001"))
        await self.store.decide("hh:900001", "rejected", "старое правило", rules_version="legacy")
        job = vacancy(id="1001", salary=Salary(200000))
        source = Probe("probe", fetch_for={"1001": job})
        await self.store.discover("probe", vacancy(id="1001"))
        engine = self.engine([source])
        await engine.tick()
        self.assertEqual((await self.store.get("hh:900001"))["status"], "rejected")
        self.assertEqual((await self.store.get("hh:900001"))["rules_version"], "legacy")
        self.assertEqual((await self.store.get("probe:1001"))["status"], "ready")
        # После включения hh строка снова видна как stale.
        self.assertEqual(await self.store.stale_row(["hh"]), "hh:900001")

    async def test_round_since_is_previous_t_start(self):
        seen = []
        class Tracking(Probe):
            async def search(self, criteria, since):
                seen.append(since)
                return await super().search(criteria, since)
        source = Tracking("probe", hits=(1001,))
        await self.store.round_request()
        engine = self.engine([source])
        await engine.run_pending_rounds()
        self.assertEqual(seen, [None])
        first_commit = await self.store.setting("last_search:probe")
        await self.store.round_request()
        await engine.run_pending_rounds()
        self.assertEqual(len(seen), 2)
        # since второго раунда — t_start предыдущего (водяной знак).
        self.assertEqual(seen[1], first_commit)
        self.assertNotEqual(await self.store.setting("last_search:probe"), first_commit)
