"""Адаптер RemoteOK (этап 6.1): публичный JSON API без входа.

GET https://remoteok.com/api — один запрос возвращает все активные вакансии
(без пагинации). ToS требуют ссылку на Remote OK как источник — карточка
всегда даёт исходную ссылку вакансии. Критерии отображаются на уровне
листинга (Python в position/tags); зарплата — USD/год, без порога по
правилам intl. `since` — водяной знак предыдущего раунда по дате публикации.
"""
import json
import re

import httpx

from ..models import RateLimited, Salary, Vacancy, VacancyUnavailable
from .base import AdapterError, RateLimiter, SearchHit, SearchReport, strip_html

API_URL = "https://remoteok.com/api"
USER_AGENT = "job_finder/0.1 (personal job search agent; link back to remoteok.com)"
PYTHON_RE = re.compile(r"\bpython\b", re.I)


def salary_from_entry(entry):
    low = entry.get("salary_min")
    high = entry.get("salary_max")
    if not low and not high:
        return None
    minimum = int(low) if low else None
    maximum = int(high) if high else None
    parts = []
    if minimum is not None:
        parts.append("от %s $" % minimum)
    if maximum is not None:
        parts.append("до %s $" % maximum)
    return Salary(minimum=minimum, maximum=maximum, currency="USD", period="year",
                  raw=" ".join(parts) + " в год")


def vacancy_from_entry(entry):
    """Листинг RemoteOK уже содержит полное описание вакансии."""
    tags = entry.get("tags") or []
    description = strip_html(entry.get("description") or "")
    return Vacancy(
        id=str(entry.get("id") or ""),
        title=strip_html(entry.get("position") or ""),
        company=strip_html(entry.get("company") or ""),
        description=description,
        requirements=", ".join(strip_html(tag) for tag in tags if tag),
        salary=salary_from_entry(entry),
        remote=True,
        remote_geo=strip_html(entry.get("location") or "") or None,
        published_at=entry.get("date"),
        language="en",
        source_url=entry.get("url") or entry.get("apply_url"),
    )


class RemoteOKAdapter:
    """Один источник = один файл + строка SOURCES=remoteok."""

    name = "remoteok"

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

    async def _listing(self):
        client = await self._client()
        await self.limiter.pause()
        try:
            response = await client.get(API_URL)
        finally:
            self.limiter.mark()
        if response.status_code == 429:
            raise RateLimited("RemoteOK ограничил запросы")
        if response.status_code != 200:
            raise AdapterError("RemoteOK отклонил запрос (HTTP %d)" % response.status_code)
        try:
            body = response.json()
        except ValueError:
            raise AdapterError("RemoteOK вернул некорректный JSON") from None
        if not isinstance(body, list):
            raise AdapterError("RemoteOK вернул некорректный ответ")
        entries = [entry for entry in body if isinstance(entry, dict) and entry.get("id")]
        self._cache = {str(entry["id"]): entry for entry in entries}
        return entries

    def _matches(self, entry):
        text = (entry.get("position") or "") + " " + " ".join(entry.get("tags") or [])
        return bool(PYTHON_RE.search(text))

    async def search(self, criteria=None, since=None):
        entries = await self._listing()
        hits = []
        for entry in entries:
            if not self._matches(entry):
                continue
            if since and (entry.get("date") or "") < since:
                continue
            hits.append(SearchHit(native_id=str(entry["id"])))
        notes = []
        if since is None:
            notes.append("первый обход: вся выдача RemoteOK")
        return SearchReport(hits=hits, incomplete=False, notes=notes)

    async def fetch(self, native_id):
        entry = self._cache.get(str(native_id))
        if entry is None:
            # Перезапуск между search и fetch: одно освежение листинга.
            await self._listing()
            entry = self._cache.get(str(native_id))
        if entry is None:
            raise VacancyUnavailable("Вакансия недоступна на RemoteOK")
        return vacancy_from_entry(entry)
