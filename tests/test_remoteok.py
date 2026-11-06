import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from job_finder.adapters.remoteok import RemoteOKAdapter, salary_from_entry, vacancy_from_entry
from job_finder.config import Config
from job_finder.models import RateLimited, Vacancy, VacancyUnavailable

ENTRIES = [
    {"id": "1001", "slug": "python-1", "position": "Senior Python Developer",
     "company": "Acme", "description": "<p>Build APIs with <b>Python</b></p>",
     "tags": ["python", "django", "remote"], "salary_min": 90000, "salary_max": 120000,
     "location": "Worldwide", "date": "2026-11-04T00:00:00+00:00",
     "url": "https://remoteok.com/remote-jobs/1001"},
    {"id": "1002", "slug": "java-1", "position": "Java Developer",
     "company": "Beta", "description": "Java", "tags": ["java"],
     "salary_min": 0, "salary_max": 0, "location": "US Only",
     "date": "2026-11-05T00:00:00+00:00", "url": "https://remoteok.com/remote-jobs/1002"},
    {"id": "1003", "slug": "python-2", "position": "Backend Engineer",
     "company": "Gamma", "description": "services", "tags": ["python", "fastapi"],
     "salary_min": 70000, "salary_max": 0, "location": "",
     "date": "2026-11-02T00:00:00+00:00", "url": "https://remoteok.com/remote-jobs/1003"},
]


def responder(request):
    return httpx.Response(200, json=ENTRIES)


class RemoteOKTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(__import__("os").environ, {}, clear=True):
            self.config = Config.load(Path(self.temp.name))
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        self.addAsyncCleanup(self.client.aclose)
        self.adapter = RemoteOKAdapter(self.config, http_client=self.client)

    async def test_search_filters_python_and_honors_since(self):
        report = await self.adapter.search(None, None)
        ids = [hit.native_id for hit in report.hits]
        self.assertEqual(ids, ["1001", "1003"])  # Java отфильтрован на листинге
        self.assertFalse(report.incomplete)
        # since — водяной знак: только новее 04.11.
        report = await self.adapter.search(None, "2026-11-03T00:00:00+00:00")
        ids = [hit.native_id for hit in report.hits]
        self.assertEqual(ids, ["1001"])

    async def test_fetch_maps_vacancy(self):
        await self.adapter.search(None, None)
        vacancy = await self.adapter.fetch("1001")
        self.assertIsInstance(vacancy, Vacancy)
        self.assertEqual(vacancy.title, "Senior Python Developer")
        self.assertEqual(vacancy.company, "Acme")
        self.assertEqual(vacancy.description, "Build APIs with Python")
        self.assertEqual(vacancy.requirements, "python, django, remote")
        self.assertEqual((vacancy.salary.minimum, vacancy.salary.maximum,
                          vacancy.salary.currency, vacancy.salary.period),
                         (90000, 120000, "USD", "year"))
        self.assertTrue(vacancy.remote)
        self.assertEqual(vacancy.remote_geo, "Worldwide")
        self.assertEqual(vacancy.language, "en")
        self.assertEqual(vacancy.url, "https://remoteok.com/remote-jobs/1001")

    async def test_fetch_refreshes_cache_after_restart(self):
        await self.adapter.search(None, None)
        self.adapter._cache.clear()  # имитация перезапуска между search и fetch
        vacancy = await self.adapter.fetch("1003")
        self.assertEqual(vacancy.company, "Gamma")
        self.assertEqual(vacancy.salary.minimum, 70000)  # «от 70000 $» без верха
        self.assertIsNone(vacancy.salary.maximum)

    async def test_fetch_missing_vacancy_raises_unavailable(self):
        await self.adapter.search(None, None)
        with self.assertRaises(VacancyUnavailable):
            await self.adapter.fetch("9999")

    async def test_rate_limited(self):
        def limited(request):
            return httpx.Response(429, json={})
        adapter = RemoteOKAdapter(self.config, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(limited)))
        with self.assertRaises(RateLimited):
            await adapter.search(None, None)
        await adapter.close()

    async def test_bad_response_is_sanitized(self):
        def broken(request):
            return httpx.Response(200, text="not-json-<secret>")
        adapter = RemoteOKAdapter(self.config, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(broken)))
        from job_finder.adapters.base import AdapterError
        with self.assertRaises(AdapterError) as caught:
            await adapter.search(None, None)
        self.assertNotIn("secret", str(caught.exception))
        await adapter.close()

    def test_salary_parsing(self):
        self.assertIsNone(salary_from_entry({"salary_min": 0, "salary_max": 0}))
        salary = salary_from_entry({"salary_min": 90000, "salary_max": 120000})
        self.assertEqual((salary.minimum, salary.maximum, salary.currency), (90000, 120000, "USD"))
        self.assertEqual(vacancy_from_entry(ENTRIES[0]).url, "https://remoteok.com/remote-jobs/1001")
