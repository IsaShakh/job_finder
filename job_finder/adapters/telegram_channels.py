"""Адаптер Telegram-каналов (этап 6.4): Telethon (MTProto), только чтение.

Один класс, несколько экземпляров по TG_CHANNELS (канал = экземпляр, 16.1):
источник = `telegram:<channel>`, native_id = message_id, полный ID
`telegram:<channel>:<message>` парсится по первому `:` (N14). Первый обход —
последние TG_FIRST_READ_LIMIT постов с честной пометкой неполноты; дальше —
чтение до водяного знака `since` с перекрытием TG_OVERLAP. Реклама
отсеивается детерминированно. Сессия — .state/tg_probe.session (0600),
ключи — TG_API_ID/TG_API_HASH из .env.
"""
import re
from datetime import timezone

from ..models import Vacancy, VacancyUnavailable
from ..rules import parse_salary
from .base import AdapterError, SearchHit, SearchReport, strip_html

TG_FIRST_READ_LIMIT = 50
TG_OVERLAP = 5

AD_MARKERS = (
    re.compile(r"^\s*[🔴🟢🔵🟡⚪]*\s*реклама\b", re.I),
    re.compile(r"\b(?:otus\.ru|shortcut\.io|updatecv\.me|продвижение|взаимопиар)\b", re.I),
)
REMOTE_RE = re.compile(r"удал[её]н\w*|remote", re.I)
HYBRID_RE = re.compile(r"гибрид\w*|hybrid", re.I)
OFFICE_RE = re.compile(r"офис\w*|office", re.I)
COMPANY_RE = re.compile(r"(?:компания|company)\s*[:\-–]\s*([^\n|]{2,60})", re.I)
SALARY_WINDOW_RE = re.compile(r"(?:зарплата|зп|оплата|salary)\s*[:\-–]?\s*([^.\n]{0,70})", re.I)
GROSS_RE = re.compile(r"до вычета|gross", re.I)
NET_RE = re.compile(r"на руки|net", re.I)
TAG_RE = re.compile(r"#([\wа-яё]+)", re.I)
MOSCOW_STOP = ("московская область", "московская обл", "подмосковье")
CYRILLIC_RE = re.compile(r"[а-яё]", re.I)
LATIN_RE = re.compile(r"[a-z]", re.I)


def detect_language(text):
    """Язык поста: кириллица → ru, иначе латиница → en; без букв — ru.

    Смешанные посты (русский текст с латинскими названиями) считаются ru.
    Writer v3 определяет язык письма по тексту вакансии независимо, это
    поле — честная разметка карточки."""
    if CYRILLIC_RE.search(text or ""):
        return "ru"
    if LATIN_RE.search(text or ""):
        return "en"
    return "ru"


def load_credentials(config):
    """TG_API_ID/TG_API_HASH из .env или окружения."""
    import os
    from dotenv import dotenv_values
    env = {**dotenv_values(config.root / ".env", interpolate=False), **os.environ}
    api_id = env.get("TG_API_ID", "").strip()
    api_hash = env.get("TG_API_HASH", "").strip()
    if not api_id or not api_hash:
        raise AdapterError("Для telegram-каналов нужны TG_API_ID и TG_API_HASH (my.telegram.org)")
    return int(api_id), api_hash


def is_ad(text):
    return any(marker.search(text or "") for marker in AD_MARKERS)


def _moscow_in(text):
    lowered = (text or "").lower()
    for stop in MOSCOW_STOP:
        lowered = lowered.replace(stop, " ")
    return "москв" in lowered or "moscow" in lowered


def _utc_iso(message):
    if message.date is None:
        return None
    return message.date.astimezone(timezone.utc).isoformat()


def vacancy_from_message(channel, message):
    # Текст поста — обычный текст (маркдаун каналов сохраняется как есть),
    # поэтому НЕ чистим через strip_html: важны переводы строк для парсинга.
    text = (message.text or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = lines[0][:300] if lines else ""
    title = re.sub(r"[*_]{1,3}", "", title).strip()
    company = ""
    match = COMPANY_RE.search(text)
    if match:
        company = re.sub(r"[*_]{1,3}", "", match.group(1)).strip()
    salary = None
    gross = None
    match = SALARY_WINDOW_RE.search(text)
    if match:
        window = match.group(1)
        salary = parse_salary(window)
        if salary is not None and salary.minimum is None and salary.maximum is None:
            salary = None
        if GROSS_RE.search(window):
            gross = True
        elif NET_RE.search(window):
            gross = False
    remote = None
    if REMOTE_RE.search(text):
        remote = True
    elif HYBRID_RE.search(text) or OFFICE_RE.search(text):
        remote = False
    return Vacancy(
        id=str(message.id),
        title=title,
        company=company,
        description=text,
        requirements=", ".join(TAG_RE.findall(text)),
        salary=salary,
        remote=remote,
        location="Москва" if _moscow_in(text) else None,
        salary_gross=gross,
        published_at=_utc_iso(message),
        language=detect_language(text),
        source_url="https://t.me/%s/%s" % (channel, message.id),
    )


class TelegramChannelsAdapter:
    """Экземпляр на канал; общий Telethon-клиент — через client_factory."""

    def __init__(self, config, channel, region="RU", client_factory=None):
        self.name = "telegram:" + channel
        self.channel = channel
        self.region = region
        self.config = config
        self._factory = client_factory
        self._client = None
        self._closed = False

    async def _ensure_client(self):
        if self._client is None:
            if self._factory is None:
                raise AdapterError("Telethon-клиент не настроен для канала " + self.channel)
            self._client = await self._factory()
        return self._client

    async def close(self):
        if not self._closed:
            self._closed = True
            if self._factory is not None:
                await self._factory(close=True)

    async def search(self, criteria=None, since=None):
        client = await self._ensure_client()
        entity = await client.get_entity(self.channel)
        limit = TG_FIRST_READ_LIMIT if since is None else None
        hits = []
        collected = 0
        async for message in client.iter_messages(entity, limit=limit):
            if not message.text:
                continue
            collected += 1
            published = _utc_iso(message)
            if since and collected > TG_OVERLAP and published and published <= since:
                break  # дошли до водяного знака с перекрытием (16.1)
            if is_ad(message.text):
                continue
            hits.append(SearchHit(native_id=str(message.id)))
        notes = []
        if since is None:
            notes.append("первый обход: прочитаны последние %s постов; история канала не читалась полностью" % TG_FIRST_READ_LIMIT)
        return SearchReport(hits=hits, incomplete=(since is None), notes=notes)

    async def fetch(self, native_id):
        # native_id после первого ':' — либо message_id, либо channel:message_id.
        channel, message_id = self.channel, str(native_id)
        if ":" in message_id:
            channel, message_id = message_id.split(":", 1)
        client = await self._ensure_client()
        entity = await client.get_entity(channel)
        messages = await client.get_messages(entity, ids=[int(message_id)])
        if not messages or messages[0] is None or not messages[0].text:
            raise VacancyUnavailable("Пост недоступен в канале")
        return vacancy_from_message(channel, messages[0])
