"""Адаптер ancor.ru/vacancy (вторая волна источников, 06.11.2026).

1С-Битрикс с серверным рендером: листинг отдаёт карточки
`<article class="b-card">` (заголовок, ссылка `/vacancy/job/<id>/` и полное
описание в атрибуте `data-text`), детальные страницы содержат JSON-LD
`JobPosting` (datePosted, город, зарплата, тип занятости).

robots.txt запрещает URL с query-параметрами (`Disallow: /*?`) — пагинация
`?page=N` не используется: читаются только первые страницы листинга и
каталога ИТ без параметров, охват честно помечается `incomplete`. Дат в
листинге нет, поэтому `since` фильтрует при fetch (по datePosted), а не
при обходе. Разведка — docs/research/2026-11-06-web-platforms-probe.md.
"""
import json
import re
from datetime import datetime, timezone
from html import unescape

import httpx

from ..models import RateLimited, Salary, Vacancy, VacancyUnavailable
from ..rules import parse_salary
from .base import AdapterError, RateLimiter, SearchHit, SearchReport

BASE_URL = "https://ancor.ru"
USER_AGENT = "job_finder/0.1 (personal job search agent; ancor.ru/vacancy)"
# Без query-параметров (robots.txt): первая страница общего списка и каталог ИТ.
LIST_PAGES = ("/vacancy/", "/vacancy/catalog/informatsionnye-tekhnologii/")
CARD_RE = re.compile(r'<a href="(/vacancy/job/[^"]+/)"[^>]*>(.*?)</a>', re.S)
DATA_TEXT_RE = re.compile(r'data-text="([^"]*)"')
JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _html_to_text(text):
    """CDATA-описание JSON-LD: <br> → перевод строки, теги срезаются."""
    text = (text or "").replace("<![CDATA[", "").replace("]]>", "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", unescape(text))
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _normalize_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _salary_from_jobposting(posting):
    base = posting.get("baseSalary") or {}
    if not isinstance(base, dict):
        return None
    value = str(base.get("value", "") or "").strip()
    currency = str(base.get("currency", "") or "").strip()
    if not value or not currency:
        return None
    salary = parse_salary("%s %s" % (value, currency))
    if salary is None or (salary.minimum is None and salary.maximum is None):
        return None
    unit = str(base.get("unitText", "") or "").strip().lower()
    if unit not in ("month", "year", "hour"):
        unit = "month"
    return Salary(minimum=salary.minimum, maximum=salary.maximum,
                  currency=currency, period=unit,
                  raw="%s %s за %s" % (value, currency, unit))


def vacancy_from_jobposting(native_id, posting):
    """JSON-LD JobPosting → каноническая Vacancy."""
    description = _html_to_text(posting.get("description"))
    location = None
    job_location = posting.get("jobLocation") or {}
    if isinstance(job_location, dict):
        address = job_location.get("address") or {}
        if isinstance(address, dict) and address.get("addressLocality"):
            location = str(address["addressLocality"])
    return Vacancy(
        id=str(native_id),
        title=unescape(str(posting.get("title") or "")).strip(),
        description=description,
        company="",   # организатор — агентство Ancor; работодатель — в тексте
        salary=_salary_from_jobposting(posting),
        remote=None,
        location=location,
        requirements="",
        published_at=_normalize_date(posting.get("datePosted")),
        language="ru",
        source_url=BASE_URL + "/vacancy/job/" + str(native_id) + "/",
    )


def listing_hits(html, page_url):
    """Карточки листинга → SearchHit по native_id (идентификатор из URL)."""
    hits = []
    for card in html.split('<article class="b-card">')[1:]:
        match = CARD_RE.search(card)
        if not match:
            continue
        native_id = match.group(1).rstrip("/").rsplit("/", 1)[-1]
        if native_id:
            hits.append(SearchHit(native_id=native_id))
    return hits


class AncorAdapter:
    """Один источник = один файл + строка SOURCES=ancor."""

    name = "ancor"

    def __init__(self, config, http_client=None):
        self.config = config
        self.http_client = http_client
        self.owns_client = http_client is None
        self.limiter = RateLimiter(1.0)

    async def _client(self):
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=30, follow_redirects=True,
                                                 headers={"User-Agent": USER_AGENT})
        return self.http_client

    async def close(self):
        if self.owns_client and self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def _get(self, url):
        client = await self._client()
        await self.limiter.pause()
        try:
            response = await client.get(url)
        finally:
            self.limiter.mark()
        if response.status_code == 429:
            raise RateLimited("ancor.ru ограничил запросы")
        if response.status_code == 404:
            raise VacancyUnavailable("Вакансия закрыта или удалена на ancor.ru")
        if response.status_code != 200:
            raise AdapterError("ancor.ru отклонил запрос (HTTP %d)" % response.status_code)
        return response.text

    async def search(self, criteria=None, since=None):
        hits = []
        seen = set()
        for path in LIST_PAGES:
            html = await self._get(BASE_URL + path)
            for hit in listing_hits(html, path):
                if hit.native_id not in seen:
                    seen.add(hit.native_id)
                    hits.append(hit)
        return SearchReport(
            hits=hits, incomplete=True,
            notes=["только первые страницы без query-параметров (robots.txt: /*?); "
                   "пагинация и история не читаются; даты публикаций — при fetch"])

    async def fetch(self, native_id):
        html = await self._get(BASE_URL + "/vacancy/job/" + str(native_id) + "/")
        match = JSONLD_RE.search(html)
        if not match:
            raise AdapterError("Страница ancor.ru изменила структуру: нет JobPosting")
        try:
            posting = json.loads(match.group(1))
        except ValueError:
            raise AdapterError("ancor.ru вернул некорректный JobPosting") from None
        if not isinstance(posting, dict) or not posting.get("title"):
            raise VacancyUnavailable("Вакансия недоступна на ancor.ru")
        return vacancy_from_jobposting(native_id, posting)
