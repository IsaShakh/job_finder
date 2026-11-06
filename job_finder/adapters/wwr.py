"""Адаптер We Work Remotely (этап 6.3): RSS-лента категории programming.

https://weworkremotely.com/categories/remote-programming-jobs.rss —
последние вакансии целиком (без пагинации); лимиты — один запрос за раунд.
Критерии отображаются на листинге (Python в названии/описании); зарплата —
USD/год, достаётся из текста описания, если указана. `region` ленты идёт
в remote_geo (правила решают территориальные ограничения).
"""
import re

import httpx

from ..models import RateLimited, Salary, Vacancy, VacancyUnavailable
from .base import AdapterError, RateLimiter, SearchHit, SearchReport, parse_rss, rfc822_to_iso, strip_html

FEED_URL = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
USER_AGENT = "job_finder/0.1 (personal job search agent)"
PYTHON_RE = re.compile(r"\bpython\b", re.I)
SALARY_RANGE_RE = re.compile(r"\$\s?([\d,]{2,})\s*(?:-|–|—|to)\s*\$?\s*([\d,]{2,})", re.I)
SALARY_SINGLE_RE = re.compile(r"\$\s?([\d,]{2,})", re.I)


def salary_from_item(item):
    description = strip_html(item.get("description") or "")
    match = SALARY_RANGE_RE.search(description)
    if match:
        low = int(match.group(1).replace(",", ""))
        high = int(match.group(2).replace(",", ""))
        return Salary(low, high, "USD", "year", "от %s $ до %s $ в год" % (low, high))
    match = SALARY_SINGLE_RE.search(description)
    if match:
        high = int(match.group(1).replace(",", ""))
        return Salary(None, high, "USD", "year", "до %s $ в год" % high)
    return None


def vacancy_from_item(item):
    title = strip_html(item.get("title") or "")
    company = ""
    if ":" in title:
        company, title = [part.strip() for part in title.split(":", 1)]
    native_id = (item.get("guid") or item.get("link") or "").rstrip("/").rsplit("/", 1)[-1]
    return Vacancy(
        id=native_id,
        title=title,
        company=company,
        description=strip_html(item.get("description") or ""),
        requirements=strip_html(item.get("category") or ""),
        salary=salary_from_item(item),
        remote=True,
        remote_geo=strip_html(item.get("region") or "") or None,
        published_at=rfc822_to_iso(item.get("pubDate") or ""),
        language="en",
        source_url=item.get("link") or item.get("guid") or None,
    )


class WWRAdapter:
    """Один источник = один файл + строка SOURCES=wwr."""

    name = "wwr"

    def __init__(self, config, http_client=None):
        self.config = config
        self.http_client = http_client
        self.owns_client = http_client is None
        self.limiter = RateLimiter(1.0)
        self._cache = {}

    async def _client(self):
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=30, follow_redirects=True,
                                                 headers={"User-Agent": USER_AGENT})
        return self.http_client

    async def close(self):
        if self.owns_client and self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def _feed(self):
        client = await self._client()
        await self.limiter.pause()
        try:
            response = await client.get(FEED_URL)
        finally:
            self.limiter.mark()
        if response.status_code == 429:
            raise RateLimited("We Work Remotely ограничил запросы")
        if response.status_code != 200:
            raise AdapterError("We Work Remotely отклонил запрос (HTTP %d)" % response.status_code)
        items = parse_rss(response.text)
        for item in items:
            native_id = (item.get("guid") or item.get("link") or "").rstrip("/").rsplit("/", 1)[-1]
            self._cache[native_id] = item
        return items

    @staticmethod
    def _matches(item):
        text = (item.get("title") or "") + " " + strip_html(item.get("description") or "")
        return bool(PYTHON_RE.search(text))

    async def search(self, criteria=None, since=None):
        items = await self._feed()
        hits = []
        oldest = None
        for item in items:
            published = rfc822_to_iso(item.get("pubDate") or "")
            if published and (oldest is None or published < oldest):
                oldest = published
            if not self._matches(item):
                continue
            if since and (published or "") < since:
                continue
            native_id = (item.get("guid") or item.get("link") or "").rstrip("/").rsplit("/", 1)[-1]
            hits.append(SearchHit(native_id=native_id))
        incomplete = since is None or (oldest is not None and since < oldest)
        notes = ["RSS: доступна только недавняя история выдачи"] if incomplete else []
        return SearchReport(hits=hits, incomplete=incomplete, notes=notes)

    async def fetch(self, native_id):
        item = self._cache.get(str(native_id))
        if item is None:
            await self._feed()  # перезапуск между search и fetch
            item = self._cache.get(str(native_id))
        if item is None:
            raise VacancyUnavailable("Вакансия недоступна на We Work Remotely")
        return vacancy_from_item(item)
