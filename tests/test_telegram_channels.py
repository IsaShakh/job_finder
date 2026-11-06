import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from job_finder.adapters.telegram_channels import (TelegramChannelsAdapter, is_ad,
                                                   vacancy_from_message)
from job_finder.config import Config
from job_finder.models import Vacancy, VacancyUnavailable

BASE_DATE = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


class FakeMessage:
    def __init__(self, message_id, text, age_hours=0):
        self.id = message_id
        self.text = text
        self.date = BASE_DATE - timedelta(hours=age_hours)


class FakeClient:
    """Минимальная замена Telethon-клиента для тестов без сети."""
    def __init__(self, messages_by_channel):
        self.messages = messages_by_channel
        self.disconnected = False

    async def get_entity(self, channel):
        return SimpleNamespace(title=channel)

    def iter_messages(self, entity, limit=None):
        messages = self.messages[entity.title]
        if limit is not None:
            messages = messages[:limit]

        async def generate():
            for message in messages:
                yield message
        return generate()

    async def get_messages(self, entity, ids):
        wanted = set(ids)
        return [message for message in self.messages[entity.title] if message.id in wanted]

    async def disconnect(self):
        self.disconnected = True


class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


VACANCY_POST = ("**Python разработчик**\n"
                "Формат: удалённо\n"
                "Компания: Акме\n"
                "Зарплата: от 300 до 450 тыс руб на руки\n"
                "#middle #python #fastapi\n"
                "Откликнуться: https://hh.ru/vacancy/123")

OFFICE_POST = ("**Python разработчик** Москва\n"
               "Формат: офис\n"
               "Компания: Бета\n"
               "#python")

AD_POST = ("Реклама\nКурсы Python со скидкой: otus.ru/python")

MOSCOW_OBLAST_POST = ("**Python разработчик**\nФормат: удалённо\nМосковская область\n#python")


class ChannelAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(__import__("os").environ, {}, clear=True):
            self.config = Config.load(Path(self.temp.name))

    def adapter(self, channel="nopremote", messages=None, region="RU"):
        client = FakeClient({channel: messages or []})
        holder = {"client": client}

        async def factory(close=False):
            if close:
                holder.pop("client", None)
                await client.disconnect()
                return None
            return client

        return TelegramChannelsAdapter(self.config, channel, region, factory), client

    async def test_first_search_reads_limit_and_marks_incomplete(self):
        messages = [FakeMessage(100 - index, VACANCY_POST, age_hours=index) for index in range(60)]
        adapter, client = self.adapter(messages=messages)
        report = await adapter.search(None, None)
        self.assertTrue(report.incomplete)
        self.assertEqual(len(report.hits), 50)  # лимит первого обхода
        self.assertTrue(any("первый обход" in note for note in report.notes))

    async def test_subsequent_search_stops_after_since_with_overlap(self):
        messages = [FakeMessage(200 - index, VACANCY_POST, age_hours=index) for index in range(61)]
        adapter, client = self.adapter(messages=messages)
        # since = 04.11T00:00 (60 часов назад): читаем новее + перекрытие 5.
        report = await adapter.search(None, "2026-11-04T00:00:00+00:00")
        self.assertFalse(report.incomplete)
        self.assertEqual(len(report.hits), 60)

    async def test_ad_posts_are_filtered(self):
        adapter, client = self.adapter(messages=[FakeMessage(1, VACANCY_POST), FakeMessage(2, AD_POST)])
        report = await adapter.search(None, None)
        self.assertEqual([hit.native_id for hit in report.hits], ["1"])

    async def test_fetch_parses_vacancy_fields(self):
        adapter, client = self.adapter(messages=[FakeMessage(42, VACANCY_POST)])
        vacancy = await adapter.fetch("42")
        self.assertIsInstance(vacancy, Vacancy)
        self.assertEqual(vacancy.title, "Python разработчик")
        self.assertEqual(vacancy.company, "Акме")
        self.assertEqual((vacancy.salary.minimum, vacancy.salary.maximum,
                          vacancy.salary.currency), (300000, 450000, "RUB"))
        self.assertEqual(vacancy.salary_gross, False)  # «на руки»
        self.assertTrue(vacancy.remote)
        self.assertIsNone(vacancy.location)  # город в посте не указан
        self.assertIn("fastapi", vacancy.requirements)
        self.assertEqual(vacancy.url, "https://t.me/nopremote/42")

    async def test_office_post_and_moscow_oblast_subtraction(self):
        adapter, client = self.adapter(messages=[FakeMessage(7, OFFICE_POST)])
        vacancy = await adapter.fetch("7")
        self.assertFalse(vacancy.remote)
        self.assertEqual(vacancy.location, "Москва")
        adapter, client = self.adapter(messages=[FakeMessage(8, MOSCOW_OBLAST_POST)])
        vacancy = await adapter.fetch("8")
        self.assertIsNone(vacancy.location)  # N10: область вычитается

    async def test_fetch_compound_native_id_and_missing(self):
        adapter, client = self.adapter(messages=[FakeMessage(5, VACANCY_POST)])
        vacancy = await adapter.fetch("nopremote:5")
        self.assertEqual(vacancy.id, "5")
        with self.assertRaises(VacancyUnavailable):
            await adapter.fetch("999")

    async def test_close_disconnects_shared_client(self):
        adapter, client = self.adapter(messages=[FakeMessage(1, VACANCY_POST)])
        await adapter.search(None, None)
        await adapter.close()
        self.assertTrue(client.disconnected)

    def test_ad_markers(self):
        self.assertTrue(is_ad("Реклама\nкурсы"))
        self.assertTrue(is_ad("Курс: otus.ru/python"))
        self.assertFalse(is_ad(VACANCY_POST))

    def test_fixtures_map_without_crash_and_find_salaries(self):
        # Реальные фикстуры из разведки 06.11 — маппинг не должен падать.
        fixtures = Path(__file__).resolve().parents[1] / "docs" / "research" / "fixtures" / "tg_channels"
        parsed = 0
        with_salary = 0
        for fixture in sorted(fixtures.glob("*.json")):
            if fixture.name == "report.json":
                continue
            for post in json.loads(fixture.read_text()):
                message = FakeMessage(post["id"], post["text"])
                vacancy = vacancy_from_message(fixture.stem, message)
                self.assertTrue(vacancy.title or vacancy.description)
                parsed += 1
                if vacancy.salary is not None:
                    with_salary += 1
        self.assertGreater(parsed, 15)
        self.assertGreater(with_salary, 0)

    def test_registry_builds_channel_instances(self):
        with patch.dict(__import__("os").environ,
                        {"SOURCES": "telegram", "TG_CHANNELS": "nopremote:RU,remotegeekjob:intl"},
                        clear=True):
            config = Config.load(Path(self.temp.name))
        from job_finder.adapters import build_adapters
        from job_finder.adapters.base import AdapterError
        with patch("job_finder.adapters.telegram_channels.load_credentials",
                   side_effect=AdapterError("нет ключей")):
            # Ключи проверяются лениво — при создании клиента, не при сборке.
            adapters = build_adapters(config)
        self.assertEqual([adapter.name for adapter in adapters],
                         ["telegram:nopremote", "telegram:remotegeekjob"])
        self.assertEqual([adapter.region for adapter in adapters], ["ru", "intl"])


class EngineChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(__import__("os").environ, {}, clear=True):
            self.config = Config.load(Path(self.temp.name))
        from job_finder.async_store import AsyncStore
        self.store = AsyncStore(self.config.db_path)
        await self.store.start()
        self.addAsyncCleanup(self.store.close)

    async def test_round_creates_compound_keys_and_prepare_routes_channel(self):
        from job_finder.engine import Engine
        from job_finder.models import Letter, WriterResult

        client = FakeClient({"nopremote": [FakeMessage(42, VACANCY_POST)]})
        holder = {"client": client}

        async def factory(close=False):
            if close:
                return None
            return client

        adapter = TelegramChannelsAdapter(self.config, "nopremote", "RU", factory)

        class Writer:
            async def generate(self, job):
                return WriterResult(score=75, score_reason="ок", score_status="ok",
                                    letter=Letter("Письмо", ["python_backend"], [], []),
                                    letter_status="ok", advice=[], advice_status="ok")

        engine = Engine(self.config, self.store, [adapter], Writer())
        await self.store.round_request()
        await engine.run_pending_rounds()
        row = await self.store.get("telegram:nopremote:42")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "found")
        await engine.prepare("telegram:nopremote:42")
        row = await self.store.get("telegram:nopremote:42")
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["vacancy"].company, "Акме")
        # Прогресс по экземпляру канала (16.1).
        self.assertIsNotNone(await self.store.setting("last_search:telegram:nopremote"))
        self.assertIsNotNone(await self.store.setting("search_report:telegram:nopremote"))
