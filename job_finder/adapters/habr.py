"""Адаптер Habr Career (этап 6.2): только RSS (официальный API запрещает
хранение материалов — решение 03.11.2026).

Лента https://career.habr.com/vacancies/rss?type=all отдаёт последние
вакансии целиком (без пагинации); лимиты — один запрос за раунд.
Критерии отображаются на уровне листинга (Python в названии/описании);
зарплату лента не несёт — решение за правилами (грейд/стек). Охват
помечается честно: история вне окна ленты — `incomplete`.
"""
import re

import httpx

from ..models import RateLimited, Vacancy, VacancyUnavailable
from .base import AdapterError, RateLimiter, SearchHit, SearchReport, parse_rss, rfc822_to_iso, strip_html

FEED_URL = "https://career.habr.com/vacancies/rss?type=all"
USER_AGENT = "job_finder/0.1 (personal job search agent)"
PYTHON_RE = re.compile(r"\bpython\b|\bпитон\w*", re.I)
REMOTE_RE = re.compile(r"удал[её]нно", re.I)
SKILLS_RE = re.compile(r"#([\wа-яё]+)", re.I)
CITY_RE = re.compile(r"\(([^)]{2,40})\)")


def vacancy_from_item(item):
    title = strip_html(item.get("title") or "")
    description = strip_html(item.get("description") or "")
    city_match = CITY_RE.search(title)
    location = city_match.group(1).strip() if city_match else None
    return Vacancy(
        id=str(item.get("guid") or ""),
        title=title,
        company=strip_html(item.get("author") or ""),
        description=description,
        requirements=", ".join(SKILLS_RE.findall(description)),
        remote=True if REMOTE_RE.search(description) else None,
        location=location,
        published_at=rfc822_to_iso(item.get("pubDate") or ""),
        language="ru",
        source_url=item.get("link") or None,
    )


class HabrAdapter:
    """Один источник = один файл + строка SOURCES=habr."""

    name = "habr"

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
            raise RateLimited("Habr Career ограничил запросы")
        if response.status_code != 200:
            raise AdapterError("Habr Career отклонил запрос (HTTP %d)" % response.status_code)
        items = parse_rss(response.text)
        self._cache = {item.get("guid"): item for item in items if item.get("guid")}
        return items

    @staticmethod
    def _matches(item):
        text = (item.get("title") or "") + " " + (item.get("description") or "")
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
            hits.append(SearchHit(native_id=str(item.get("guid"))))
        # Честность покрытия (16.5): лента ограничена окном истории.
        incomplete = since is None or (oldest is not None and since < oldest)
        notes = ["RSS: доступна только недавняя история выдачи"] if incomplete else []
        return SearchReport(hits=hits, incomplete=incomplete, notes=notes)

    async def fetch(self, native_id):
        item = self._cache.get(str(native_id))
        if item is None:
            await self._feed()  # перезапуск между search и fetch
            item = self._cache.get(str(native_id))
        if item is None:
            raise VacancyUnavailable("Вакансия недоступна на Habr Career")
        return vacancy_from_item(item)
