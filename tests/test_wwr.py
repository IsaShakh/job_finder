import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from job_finder.adapters.wwr import WWRAdapter, vacancy_from_item as wwr_vacancy
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


class WWRRssAdapterTests(unittest.IsolatedAsyncioTestCase):
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


class WWRTests(WWRRssAdapterTests):
    async def test_search_filters_python(self):
        adapter = self.adapter(WWRAdapter, WWR_RSS)
        report = await adapter.search(None, None)
        ids = [hit.native_id for hit in report.hits]
        self.assertEqual(ids, ["acme-senior-python-engineer"])

    async def test_fetch_maps_vacancy_and_salary(self):
        adapter = self.adapter(WWRAdapter, WWR_RSS)
        await adapter.search(None, None)
        vacancy = await adapter.fetch("acme-senior-python-engineer")
        self.assertEqual(vacancy.title, "Senior Python Engineer")
        self.assertEqual(vacancy.company, "Acme")
        self.assertEqual((vacancy.salary.minimum, vacancy.salary.maximum,
                          vacancy.salary.currency, vacancy.salary.period),
                         (100000, 150000, "USD", "year"))
        self.assertTrue(vacancy.remote)
        self.assertEqual(vacancy.remote_geo, "Anywhere in the World")
        self.assertEqual(vacancy.language, "en")
        self.assertEqual(vacancy.url, "https://weworkremotely.com/remote-jobs/acme-senior-python-engineer")

    def test_single_salary_is_ceiling(self):
        from job_finder.adapters.wwr import salary_from_item
        salary = salary_from_item({"description": "<p>Salary $120,000 flat.</p>"})
        self.assertEqual((salary.minimum, salary.maximum), (None, 120000))

    def test_region_foreign_only_detected_by_rules(self):
        from job_finder.config import PROFILES
        from job_finder.rules import evaluate
        vacancy = wwr_vacancy({"title": "Beta: Designer", "region": "Europe Only",
                               "description": "<p>Python backend</p>",
                               "pubDate": "Wed, 04 Nov 2026 10:00:00 +0000",
                               "link": "https://weworkremotely.com/remote-jobs/beta-designer",
                               "guid": "https://weworkremotely.com/remote-jobs/beta-designer"})
        self.assertEqual(evaluate(vacancy, PROFILES["intl"]).status, "rejected")
