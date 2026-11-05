"""Общие хелперы адаптеров источников (этап 2 плана, раздел 6).

Извлечены из `job_finder/hh.py`: rate limiter (0.4 с между запросами),
санитизация JSON-ответов, обновление OAuth-токена с атомарной записью.
Контракт адаптера — `SourceAdapter`; ошибки адаптера санитизированы:
токены, cookies и тела ответов в исключения не попадают.
"""
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..models import AttentionRequired, Vacancy


class AdapterError(ValueError):
    """Санитизированная ошибка адаптера: без токенов и тел ответов."""


@dataclass(frozen=True)
class SearchHit:
    """Дешёвое поле листинга: источник фиксирован контекстом адаптера."""
    native_id: str


@dataclass(frozen=True)
class SearchReport:
    """Результат обхода: hits + признак полноты покрытия + примечания."""
    hits: list
    incomplete: bool = False
    notes: list = field(default_factory=list)


class SourceAdapter(Protocol):
    """Контракт адаптера (раздел 6 плана).

    `search` возвращает дешёвые поля листинга и признак полноты покрытия;
    `since` — водяной знак платформы (t_start предыдущего раунда).
    `fetch(key)` получает `native_id` (источник фиксирован адаптером).
    `close` — время жизни HTTP-клиента.
    """
    name: str

    def __init__(self, config): ...

    async def search(self, criteria, since) -> SearchReport: ...

    async def fetch(self, native_id) -> Vacancy: ...

    async def close(self): ...


class TokenStore:
    """Файл токенов OAuth (0600, атомарная запись через .tmp + os.replace)."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, token):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(token), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    def clear(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class RateLimiter:
    """Интервал не меньше min_interval секунд между запросами."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._last_request = 0.0

    async def pause(self):
        remaining = self.min_interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            await asyncio.sleep(remaining)

    def mark(self):
        self._last_request = time.monotonic()


def safe_json(response):
    """JSON-тело ответа или {} — без исключений и без сырых тел."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class OAuthClient:
    """Обновление OAuth-токена с проверкой и атомарной записью.

    Ключи приложения и URL площадки задаёт наследник; `_refresh` вызывается
    при 403 с refresh_token в хранилище, ровно один раз на запрос.
    """

    oauth_token_url = None

    def __init__(self, config, token_path, http_client=None, min_interval=0.4):
        self.config = config
        self.tokens = TokenStore(token_path)
        self.http_client = http_client
        self.owns_client = http_client is None
        self.limiter = RateLimiter(min_interval)

    async def close(self):
        if self.owns_client and self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def _client(self):
        import httpx
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=40, follow_redirects=False)
        return self.http_client

    def refresh_payload(self):
        """Данные POST для обновления токена; переопределяет наследник."""
        raise NotImplementedError

    async def _refresh(self):
        import httpx
        token = self.tokens.load()
        refresh = token.get("refresh_token")
        if not refresh:
            raise AdapterError("Токен истёк; выполните вход заново")
        client = await self._client()
        response = await client.post(self.oauth_token_url, data=self.refresh_payload(refresh))
        if response.status_code != 200:
            self.tokens.clear()
            raise AdapterError("Сервис не обновил токен; выполните вход заново")
        body = safe_json(response)
        access = body.get("access_token")
        if not isinstance(access, str):
            self.tokens.clear()
            raise AdapterError("Сервис вернул некорректный токен; выполните вход заново")
        self.tokens.save({
            "access_token": access,
            "refresh_token": body.get("refresh_token") or refresh,
            "access_expires_at": time.time() + int(body.get("expires_in") or 0),
        })
