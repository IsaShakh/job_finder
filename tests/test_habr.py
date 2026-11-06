import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from job_finder.adapters.habr import HabrAdapter, vacancy_from_item as habr_vacancy
from job_finder.config import Config
from job_finder.models import Vacancy, VacancyUnavailable

HABR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Вакансии — Хабр Карьера</title>
<item>
  <title>Требуется «Python-разработчик» (Москва)</title>
  <description>Компания ищет специалиста. Москва (Россия). Можно удалённо. Требуемые навыки: #FastAPI, #Django.</description>
  <author>Группа Астра</author>
  <pubDate>Fri, 06 Nov 2026 15:36:50 +0300</pubDate>
  <link>https://career.habr.com/vacancies/1000169321</link>
  <guid>1000169321</guid>
</item>
<item>
  <title>Требуется «Технический писатель» (Казань)</title>
  <description>Документация и ГОСТы.</description>
  <author>Другая</author>
  <pubDate>Thu, 05 Nov 2026 10:00:00 +0300</pubDate>
  <link>https://career.habr.com/vacancies/1000169320</link>
  <guid>1000169320</guid>
</item>
</channel></rss>
"""

WWR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>We Work Remotely</title>
<item>
  <title>Acme: Senior Python Engineer</title>
  <region>Anywhere in the World</region>
  <category>Full-Stack Programming</category>
  <description>&lt;p&gt;We build APIs. Salary $100,000 - $150,000.&lt;/p&gt;</description>
  <pubDate>Thu, 05 Nov 2026 10:00:00 +0000</pubDate>
  <link>https://weworkremotely.com/remote-jobs/acme-senior-python-engineer</link>
  <guid>https://weworkremotely.com/remote-jobs/acme-senior-python-engineer</guid>
</item>
<item>
  <title>Beta: Designer</title>
  <region>Europe Only</region>
  <category>Design</category>
  <description>&lt;p&gt;Design things.&lt;/p&gt;</description>
  <pubDate>Wed, 04 Nov 2026 10:00:00 +0000</pubDate>
  <link>https://weworkremotely.com/remote-jobs/beta-designer</link>
  <guid>https://weworkremotely.com/remote-jobs/beta-designer</guid>
</item>
</channel></rss>
"""


class RSSAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(__import__("os").environ, {}, clear=True):
            self.config = Config.load(Path(self.temp.name))

    def adapter(self, cls, rss):
        def responder(request):
            return httpx.Response(200, text=rss)
        client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        self.addAsyncCleanup(client.aclose)
        return cls(self.config, http_client=client)


class HabrTests(RSSAdapterTests):
    async def test_search_filters_python_and_coverage(self):
        adapter = self.adapter(HabrAdapter, HABR_RSS)
        report = await adapter.search(None, None)
        ids = [hit.native_id for hit in report.hits]
        self.assertEqual(ids, ["1000169321"])
        self.assertTrue(report.incomplete)  # первый обход — история ограничена лентой
        # since в окне ленты — покрытие полное.
        report = await adapter.search(None, "2026-11-05T12:00:00+00:00")
        self.assertEqual(len(report.hits), 1)
        self.assertFalse(report.incomplete)

    async def test_fetch_maps_vacancy(self):
        adapter = self.adapter(HabrAdapter, HABR_RSS)
        await adapter.search(None, None)
        vacancy = await adapter.fetch("1000169321")
        self.assertIsInstance(vacancy, Vacancy)
        self.assertEqual(vacancy.title, "Требуется «Python-разработчик» (Москва)")
        self.assertEqual(vacancy.company, "Группа Астра")
        self.assertTrue(vacancy.remote)
        self.assertEqual(vacancy.location, "Москва")
        self.assertIn("FastAPI", vacancy.requirements)
        self.assertEqual(vacancy.language, "ru")
        self.assertEqual(vacancy.url, "https://career.habr.com/vacancies/1000169321")

    async def test_fetch_missing_raises_unavailable(self):
        adapter = self.adapter(HabrAdapter, HABR_RSS)
        await adapter.search(None, None)
        with self.assertRaises(VacancyUnavailable):
            await adapter.fetch("000")

    def test_habr_vacancy_mapping_direct(self):
        vacancy = habr_vacancy({"title": "Python dev (Москва)", "guid": "123",
                                "author": "Компания", "description": "Можно удалённо. #FastAPI",
                                "pubDate": "Fri, 06 Nov 2026 15:36:50 +0300",
                                "link": "https://career.habr.com/vacancies/123"})
        self.assertEqual(vacancy.remote, True)
        self.assertEqual(vacancy.location, "Москва")
