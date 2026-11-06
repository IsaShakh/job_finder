import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from job_finder.adapters.ancor import (AncorAdapter, _html_to_text,
                                       listing_hits, vacancy_from_jobposting)
from job_finder.adapters.base import AdapterError
from job_finder.config import Config
from job_finder.models import RateLimited, Vacancy, VacancyUnavailable


def listing_html():
    return ("<html><body>"
            '<article class="b-card">'
            '<div class="b-card-title">'
            '<a href="/vacancy/job/6ac90ca072bcd400123f0afa/">Python-разработчик</a>'
            '<span class="b-card-info" data-text="&lt;p&gt;Обязанности:&lt;br&gt; Писать FastAPI-сервисы&lt;/p&gt;"></span>'
            "</div></article>"
            '<article class="b-card">'
            '<div class="b-card-title">'
            '<a href="/vacancy/job/aaaa1111222233334444bbbb/">Бухгалтер</a>'
            '<span data-text="без разметки"></span>'
            "</div></article>"
            "</body></html>")


def jobposting_python():
    return json.dumps({
        "@type": "JobPosting",
        "title": "Python-разработчик",
        "description": "<![CDATA[ <p><b>Обязанности:</b><br>\nПисать сервисы на FastAPI</p> ]]>\n",
        "identifier": {"@type": "PropertyValue", "value": "6ac90ca072bcd400123f0afa"},
        "datePosted": "2026-11-04T09:15:30.123Z",
        "employmentType": "FULL_TIME",
        "hiringOrganization": {"@type": "Organization", "name": "Ancor"},
        "jobLocation": {"@type": "place", "address": {
            "@type": "PostalAddress", "addressLocality": "Москва",
            "addressRegion": "Москва", "addressCountry": "Россия"}},
        "baseSalary": {"@type": "MonetaryAmount", "currency": "RUB",
                       "value": {"@type": "QuantitativeValue",
                                 "unitText": "month", "value": "от 250000"}},
    })


def detail_html(posting=None, status=200):
    if status == 404:
        return None
    posting = posting or jobposting_python()
    return "<html><head><script type=\"application/ld+json\">%s</script></head></html>" % posting


class Responder:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, request):
        self.calls.append(str(request.url))
        if request.url.path == "/vacancy/" or request.url.path.endswith("informatsionnye-tekhnologii/"):
            return httpx.Response(200, text=listing_html())
        if request.url.path.startswith("/vacancy/job/"):
            if "aaaa1111222233334444bbbb" in request.url.path:
                return httpx.Response(404)
            return httpx.Response(200, text=detail_html())
        return httpx.Response(404)


class AncorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(__import__("os").environ, {}, clear=True):
            self.config = Config.load(Path(self.temp.name))
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(Responder({})))
        self.addAsyncCleanup(self.client.aclose)
        self.adapter = AncorAdapter(self.config, http_client=self.client)

    async def test_search_reads_list_pages_and_dedupes(self):
        report = await self.adapter.search(None, None)
        ids = [hit.native_id for hit in report.hits]
        self.assertEqual(ids, ["6ac90ca072bcd400123f0afa", "aaaa1111222233334444bbbb"])
        self.assertTrue(report.incomplete)
        self.assertTrue(any("robots.txt" in note for note in report.notes))

    async def test_search_only_first_pages_no_query_params(self):
        await self.adapter.search(None, None)
        for url in self.client._transport.handler.calls:
            self.assertNotIn("?", url)  # robots.txt: /*?

    async def test_fetch_maps_jobposting(self):
        vacancy = await self.adapter.fetch("6ac90ca072bcd400123f0afa")
        self.assertIsInstance(vacancy, Vacancy)
        self.assertEqual(vacancy.title, "Python-разработчик")
        self.assertEqual(vacancy.description, "Обязанности:\nПисать сервисы на FastAPI")
        self.assertEqual(vacancy.location, "Москва")
        self.assertEqual(vacancy.published_at, "2026-11-04T09:15:30.123000+00:00")
        self.assertEqual(vacancy.salary.currency, "RUB")
        self.assertEqual(vacancy.salary.period, "month")
        self.assertEqual(vacancy.salary.minimum, 250000)
        self.assertEqual(vacancy.language, "ru")
        self.assertEqual(vacancy.url, "https://ancor.ru/vacancy/job/6ac90ca072bcd400123f0afa/")

    async def test_fetch_closed_vacancy_raises_unavailable(self):
        with self.assertRaises(VacancyUnavailable):
            await self.adapter.fetch("aaaa1111222233334444bbbb")

    async def test_fetch_without_jobposting_raises_adapter_error(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text="<html><body>без JSON-LD</body></html>"))
        client = httpx.AsyncClient(transport=transport)
        self.addAsyncCleanup(client.aclose)
        adapter = AncorAdapter(self.config, http_client=client)
        with self.assertRaises(AdapterError):
            await adapter.fetch("someid")

    async def test_rate_limited(self):
        def limited(request):
            return httpx.Response(429)
        client = httpx.AsyncClient(transport=httpx.MockTransport(limited))
        self.addAsyncCleanup(client.aclose)
        adapter = AncorAdapter(self.config, http_client=client)
        with self.assertRaises(RateLimited):
            await adapter.search(None, None)

    def test_html_to_text_preserves_breaks(self):
        self.assertEqual(_html_to_text("<![CDATA[ <p>a<br>b</p><p>c</p>]]>"), "a\nb\nc")

    def test_salary_without_value_is_none(self):
        posting = json.loads(jobposting_python())
        posting["baseSalary"]["value"]["value"] = ""
        self.assertIsNone(vacancy_from_jobposting("x", posting).salary)

    def test_vacancy_without_salary_and_location(self):
        posting = json.loads(jobposting_python())
        posting.pop("baseSalary", None)
        posting.pop("jobLocation", None)
        vacancy = vacancy_from_jobposting("x", posting)
        self.assertIsNone(vacancy.salary)
        self.assertIsNone(vacancy.location)

    def test_listing_hits_parse_cards(self):
        hits = listing_hits(listing_html(), "/vacancy/")
        self.assertEqual([h.native_id for h in hits],
                         ["6ac90ca072bcd400123f0afa", "aaaa1111222233334444bbbb"])

    def test_registry_contains_ancor(self):
        from job_finder.adapters import REGISTRY
        self.assertIs(REGISTRY["ancor"], AncorAdapter)

    def test_live_fixture_jobposting_parses(self):
        # Реальная страница вакансии из разведки 06.11 — структура не уехала.
        raw = (Path(__file__).resolve().parents[1] / "docs" / "research"
               / "fixtures" / "ancor" / "job-detail.html").read_text(encoding="utf-8")
        import re
        match = re.search(r'<script type="application/ld\+json">(.*?)</script>', raw, re.S)
        posting = json.loads(match.group(1))
        vacancy = vacancy_from_jobposting("6ac90ca072bcd400123f0afa", posting)
        self.assertEqual(vacancy.title, "Системный/бизнес-аналитик")
        self.assertEqual(vacancy.location, "Москва")
        self.assertEqual(vacancy.published_at[:10], "2026-10-09")

    def test_live_fixture_listing_parses(self):
        raw = (Path(__file__).resolve().parents[1] / "docs" / "research"
               / "fixtures" / "ancor" / "listing-it.html").read_text(encoding="utf-8")
        hits = listing_hits(raw, "/vacancy/catalog/informatsionnye-tekhnologii/")
        self.assertGreaterEqual(len(hits), 2)
        self.assertTrue(all(h.native_id for h in hits))
