"""Программный доступ к API hh.ru без браузера.

Реализован соискательский API в стиле официального Android-приложения hh.ru
(OAuth2 с ключами приложения; протокол описан в
docs/research/2026-10-30-programmatic-api.md). Используется для поиска,
чтения вакансий и отправки откликов; браузер остаётся только резервом.

Все ошибки санитизируются: токены, cookies и тела ответов в исключения
не попадают. Ключевые инварианты совпадают с браузерным адаптером:
намерение фиксируется до отправки, отклик считается отправленным только
после подтверждения площадкой, повторная отправка запрещена.
"""
import asyncio
import base64
import html
import json
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .models import AttentionRequired, Outcome, Salary, Vacancy
from .rules import evaluate

API_URL = "https://api.hh.ru"
OAUTH_URL = "https://hh.ru/oauth"
SITE_URL = "https://hh.ru"
OAUTH_REDIRECT = "hhandroid://oauthresponse"

# Публичные ключи официального Android-приложения hh.ru; клиент
# эмулирует приложение (см. ресёрч). Это не наши секреты, а константы
# приложения, поэтому они остаются в коде, а не в .env.
ANDROID_CLIENT_ID = "HIOMIAS39CA9DICTA7JIO64LQKQJF5AGIK74G9ITJKLNEDAOH5FHS5G1JI7FOEGD"
ANDROID_CLIENT_SECRET = "V9M870DE342BGHFRUJ5FTCGCUA1482AN0DI8C5TFI9ULMA89H10N60NOP8I4JMVS"

ANDROID_USER_AGENT = ("Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230901.001; wv) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/118.0.0.0 Mobile Safari/537.36")
DESKTOP_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
DESKTOP_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
DESKTOP_ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"

MIN_REQUEST_INTERVAL = 0.4
SEARCH_PER_PAGE = 100
API_MAX_RESULTS = 2000
EXPERIENCE_RANGES = ("noExperience", "between1And3", "between3And6", "moreThan6")
LOGIN_PRE_DELAY = 7.5
APPLY_MIN_DELAY = 1.0
APPLY_MAX_DELAY = 3.0
MAX_CAPTCHA_ATTEMPTS = 5


class HHAPIError(ValueError):
    """Санитизированная ошибка API hh.ru: без токенов и тел ответов."""


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


class TokenStore:
    """Файл .state/api-token.json с токенами OAuth (0600, атомарная запись)."""

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


def vacancy_from_json(data):
    """Преобразует JSON вакансии API в models.Vacancy."""
    vacancy_id = str(data.get("id") or "")
    employer = data.get("employer") or {}
    salary = data.get("salary") or {}
    parsed_salary = None
    if salary:
        currency = salary.get("currency") or "RUB"
        if currency == "RUR":
            currency = "RUB"
        low, high = salary.get("from"), salary.get("to")
        gross = " до вычета налогов" if salary.get("gross") else " на руки"
        parts = [str(low) if low is not None else "", str(high) if high is not None else ""]
        raw = "–".join(part for part in parts if part) + " " + currency + gross
        parsed_salary = Salary(
            minimum=None if low is None else int(low),
            maximum=None if high is None else int(high),
            currency=currency,
            period="month",
            raw=raw,
        )
    work_formats = [item.get("id") for item in (data.get("work_format") or [])]
    remote = None
    if work_formats:
        remote = "REMOTE" in work_formats
    elif (data.get("schedule") or {}).get("id") == "remote":
        remote = True
    relations = data.get("relations") or []
    vacancy_type = (data.get("type") or {}).get("id")
    return Vacancy(
        id=vacancy_id,
        title=data.get("name") or "",
        description=strip_html(data.get("description") or ""),
        company=employer.get("name") or "",
        salary=parsed_salary,
        remote=remote,
        test_required=bool(data.get("has_test") or (data.get("test") or {}).get("required")),
        closed=bool(data.get("archived") or vacancy_type == "closed"),
        already_applied=bool(relations),
        requirements=", ".join(item.get("name") or "" for item in (data.get("key_skills") or [])),
    )


class HHClient:
    """Клиент соискательского API; интерфейс совместим с Engine."""

    def __init__(self, config, http_client=None, min_interval=MIN_REQUEST_INTERVAL):
        self.config = config
        self.tokens = TokenStore(config.api_token_path)
        self.http_client = http_client
        self.owns_client = http_client is None
        self.min_interval = min_interval
        self._last_request = 0.0

    async def close(self):
        if self.owns_client and self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def _client(self):
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=40, follow_redirects=False)
        return self.http_client

    async def _pause(self):
        remaining = self.min_interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            await asyncio.sleep(remaining)

    def _headers(self, auth=True):
        headers = {
            "Accept": "application/json",
            "X-Force-App-Access": "true",
            "X-HH-App-Active": "true",
            "User-Agent": ANDROID_USER_AGENT,
        }
        token = self.tokens.load().get("access_token")
        if auth and token:
            headers["Authorization"] = "Bearer " + token
        return headers

    @staticmethod
    def _safe_json(response):
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _captcha(body):
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, dict) and (
                        error.get("type") == "captcha_required" or error.get("captcha_url")):
                    return True
        return False

    async def _refresh(self):
        token = self.tokens.load()
        refresh = token.get("refresh_token")
        if not refresh:
            raise HHAPIError("Токен HH истёк; выполните api-login")
        client = await self._client()
        response = await client.post(OAUTH_URL + "/token", data={
            "client_id": ANDROID_CLIENT_ID,
            "client_secret": ANDROID_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        })
        if response.status_code != 200:
            self.tokens.clear()
            raise HHAPIError("HH не обновил токен; выполните api-login")
        body = self._safe_json(response)
        access = body.get("access_token")
        if not isinstance(access, str) or not access.startswith("USER"):
            self.tokens.clear()
            raise HHAPIError("HH вернул некорректный токен; выполните api-login")
        self.tokens.save({
            "access_token": access,
            "refresh_token": body.get("refresh_token") or refresh,
            "access_expires_at": time.time() + int(body.get("expires_in") or 0),
        })

    async def _request(self, method, path, params=None, data=None, auth=True,
                       allow_status=(), retry_refresh=True):
        """Один запрос к API с паузой, обновлением токена и проверкой капчи."""
        client = await self._client()
        await self._pause()
        try:
            response = await client.request(method, API_URL + path, params=params,
                                            data=data, headers=self._headers(auth))
        finally:
            self._last_request = time.monotonic()
        body = self._safe_json(response)
        if response.status_code == 403 and self._captcha(body):
            raise AttentionRequired("HH просит проверку (капчу). Откройте hh.ru вручную или повторите позже")
        if (response.status_code == 403 and auth and retry_refresh
                and self.tokens.load().get("refresh_token")):
            try:
                await self._refresh()
            except HHAPIError:
                raise
            return await self._request(method, path, params=params, data=data,
                                       auth=auth, allow_status=allow_status, retry_refresh=False)
        if response.status_code >= 400 and response.status_code not in allow_status:
            raise HHAPIError("HH отклонил запрос API (HTTP %d)" % response.status_code)
        return response

    async def _json(self, method, path, params=None, data=None):
        response = await self._request(method, path, params=params, data=data)
        body = self._safe_json(response)
        if not body and method != "POST":
            raise HHAPIError("HH вернул некорректный ответ API")
        return body

    async def search(self, max_pages=100):
        """Поиск Python-вакансий (удалённо) с контролем полноты выдачи."""
        ids = set()
        report = {"ids": [], "pages": 0, "incomplete": False, "notes": []}
        partitions = [None]
        split = False
        while partitions:
            experience = partitions.pop(0)
            collected = set()
            expected = None
            page_limit = min(max_pages, API_MAX_RESULTS // SEARCH_PER_PAGE)
            for page in range(page_limit):
                params = {"text": "Python", "schedule": "remote",
                          "order_by": "publication_time", "per_page": SEARCH_PER_PAGE}
                if experience:
                    params["experience"] = experience
                if page:
                    params["page"] = page
                body = await self._json("GET", "/vacancies", params=params)
                expected = body.get("found")
                items = body.get("items") or []
                for item in items:
                    if isinstance(item, dict) and item.get("id"):
                        collected.add(str(item["id"]))
                report["pages"] += 1
                total_pages = body.get("pages") or 0
                if not items or page + 1 >= total_pages:
                    break
            ids.update(collected)
            capped = (expected is not None and len(collected) < expected) or (
                expected is not None and expected > API_MAX_RESULTS)
            if capped and experience is None and not split:
                split = True
                partitions.extend(EXPERIENCE_RANGES)
                report["notes"].append("Выдача ограничена лимитом API; дополнительно обходим четыре диапазона опыта")
            elif capped:
                report["incomplete"] = True
                report["notes"].append("Не вся выдача доступна: " + str(experience or "общий поиск"))
        report["ids"] = sorted(ids, key=int)
        return report

    async def read(self, job_id):
        """Читает одну вакансию; закрытые и недоступные не блокируют очередь."""
        if not str(job_id).isdigit():
            raise ValueError("Некорректный ID вакансии")
        response = await self._request("GET", "/vacancies/" + str(job_id),
                                       allow_status=(404, 410))
        if response.status_code in (404, 410):
            return Vacancy(str(job_id), "Вакансия недоступна", "", closed=True)
        data = self._safe_json(response)
        if not data.get("id"):
            raise HHAPIError("HH вернул некорректные данные вакансии")
        return vacancy_from_json(data)

    async def me(self):
        return await self._json("GET", "/me")

    async def _negotiation_ids(self, vacancy_id, max_pages=20):
        for page in range(max_pages):
            body = await self._json("GET", "/negotiations",
                                    params={"per_page": 100, "page": page})
            items = body.get("items") or []
            for item in items:
                vacancy = item.get("vacancy") if isinstance(item, dict) else None
                item_vacancy = str((vacancy or {}).get("id") or item.get("vacancy_id") or "")
                if item_vacancy == str(vacancy_id):
                    return item.get("id")
            if not items or page + 1 >= (body.get("pages") or 0):
                return None
        return None

    async def _confirm(self, vacancy_id, attempts=3):
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(2)
            negotiation_id = await self._negotiation_ids(vacancy_id)
            if negotiation_id is not None:
                return Outcome("sent", "Подтверждено списком откликов HH (отклик %s)" % negotiation_id,
                               "Отклик подтверждён API HH")
        return Outcome("unknown", reason="Отклик отправлен через API, но не найден в списке откликов HH. Проверьте историю вручную")

    async def reconcile(self, job_id):
        negotiation_id = await self._negotiation_ids(job_id)
        if negotiation_id is not None:
            return Outcome("sent", "Подтверждено списком откликов HH (отклик %s)" % negotiation_id,
                           "Отклик подтверждён API HH")
        return Outcome("unknown", reason="В списке откликов HH не найден. Проверьте историю вручную")

    @staticmethod
    def _describe_error(body):
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, dict) and error.get("type") == "negotiations-limit-exceeded":
                    return "Достигнут дневной лимит откликов HH"
                if isinstance(error, dict) and error.get("value"):
                    return str(error["value"])[:300]
        return "неизвестная причина"

    async def apply(self, vacancy, letter, before_send):
        """Отклик через POST /negotiations; подтверждение — по списку откликов."""
        attempted = False
        try:
            current = await self.read(vacancy.id)
            if current.already_applied:
                return Outcome("sent", "Отклик уже отмечен на HH", "Ранее отправленный отклик")
            decision = evaluate(current)
            if decision.status != "ready":
                return Outcome(decision.status, reason=decision.reason)
            if not await before_send():
                return Outcome("ready", reason="Пауза или режим просмотра: отправка не начата")
            # Намерение уже зафиксировано в БД (before_send выполнил claim).
            # Дальнейшие сбои не могут трактоваться как «не отправлено».
            attempted = True
            await asyncio.sleep(random.uniform(APPLY_MIN_DELAY, APPLY_MAX_DELAY))
            response = await self._request("POST", "/negotiations", data={
                "resume_id": self.config.resume_id,
                "vacancy_id": vacancy.id,
                "message": letter.text,
            })
            if response.is_redirect:
                return Outcome("needs_clarification",
                               reason="HH перенаправил на форму отклика; требуется браузер или ручной отклик")
            body = self._safe_json(response)
            if body == {}:
                return await self._confirm(vacancy.id)
            return Outcome("error", reason="HH отклонил отклик: " + self._describe_error(body))
        except AttentionRequired:
            if attempted:
                return Outcome("unknown", reason="После начала отправки HH запросил действие пользователя")
            raise
        except HHAPIError as exc:
            return Outcome("unknown" if attempted else "error", reason=str(exc))
        except Exception as exc:
            return Outcome("unknown" if attempted else "error",
                           reason="Сбой API: " + type(exc).__name__)


def parse_initial_state(text):
    """Достаёт JSON из <template id="HH-Lux-InitialState"> страницы hh.ru."""
    marker = 'id="HH-Lux-InitialState">'
    start = text.find(marker)
    if start == -1:
        raise HHAPIError("Структура страницы входа HH изменилась; повторите позже или используйте браузер")
    start += len(marker)
    end = text.find("</template>", start)
    if end == -1:
        raise HHAPIError("Структура страницы входа HH изменилась; повторите позже или используйте браузер")
    raw = text[start:end]
    if raw.startswith("{&#34;"):
        raw = html.unescape(raw)
    try:
        data = json.loads(raw)
    except ValueError:
        raise HHAPIError("Структура страницы входа HH изменилась; повторите позже или используйте браузер") from None
    if not isinstance(data, dict):
        raise HHAPIError("Структура страницы входа HH изменилась; повторите позже или используйте браузер")
    return data


def trust_flags(public_key_pem, payload=None):
    """loginTrustFlags: RSA-OAEP(SHA-256) поверх компактного JSON с ts."""
    data = {**(payload or {}), "ts": int(time.time() * 1000)}
    plaintext = json.dumps(data, separators=(",", ":")).encode()
    public_key = serialization.load_pem_public_key(public_key_pem.encode())
    ciphertext = public_key.encrypt(
        plaintext,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(ciphertext).decode()


async def _call(fn):
    """Позволяет передавать синхронные (input, солвер капчи) и асинхронные колбэки."""
    if fn is None:
        return None
    if asyncio.iscoroutinefunction(fn):
        return await fn()
    return await asyncio.to_thread(fn)


async def _xhr(client, method, url, data=None, params=None, xsrf=None, referer=None, extra=None):
    """XHR-запрос к сайту hh.ru в стиле браузера; возвращает JSON-словарь."""
    parts = urlsplit(url)
    origin = parts.scheme + "://" + parts.netloc
    headers = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-XSRFToken": xsrf,
        "Origin": origin,
        "Referer": referer or origin + "/",
        **(extra or {}),
    }
    response = await client.request(method, url, params=params, data=data,
                                    headers=headers, follow_redirects=False)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return body if isinstance(body, dict) else {}


async def _login_by_password(client, login_url, xsrf, pubkey, username, password, captcha=None):
    parts = urlsplit(login_url)
    fail_url = parts.path + ("?" + parts.query if parts.query else "")
    data = {
        "accountType": "APPLICANT",
        "failUrl": fail_url,
        "remember": "true",
        "loginTrustFlags": trust_flags(pubkey, {"password": {"suggest": True, "paste": False}}),
        "username": username,
        "password": password,
        "captchaText": "",
    }
    if captcha:
        data.update(captcha)
    return await _xhr(client, "POST", login_url, data=data, xsrf=xsrf,
                      referer=login_url, extra={"X-Hhtmfrom": "", "X-Hhtmsource": "account_login"})


async def _request_otp(client, login_url, xsrf, pubkey, username, is_email, captcha=None):
    data = {
        "login": username,
        "otpType": "email" if is_email else "phone",
        "operationType": "applicant_otp_auth",
        "authScenario": "APPLICANT_AUTH",
        "formatPhone": "true",
        "isSignupPage": "false",
        "loginTrustFlags": trust_flags(pubkey, {"emailOrPhone": {"paste": False, "suggest": True}}),
        "captchaText": "",
    }
    if captcha:
        data.update(captcha)
    return await _xhr(client, "POST", urljoin(login_url, "/account/otp_generate"), data=data,
                      xsrf=xsrf, referer=login_url,
                      extra={"X-Hhtmfrom": "", "X-Hhtmsource": "account_login"})


async def _confirm_otp(client, login_url, xsrf, pubkey, username, code):
    data = {
        "username": username,
        "code": code,
        "backurl": login_url,
        "operationType": "applicant_otp_auth",
        "isApplicantSignup": "false",
        "remember": "true",
        "loginTrustFlags": trust_flags(pubkey),
        "authScenario": "APPLICANT_AUTH",
    }
    return await _xhr(client, "POST", urljoin(login_url, "/account/login/by_code"), data=data,
                      xsrf=xsrf, referer=login_url,
                      extra={"X-Hhtmfrom": "", "X-Hhtmsource": "account_login"})


async def _captcha_solution(client, xsrf, referer, state, solve):
    result = await _xhr(client, "POST", urljoin(referer, "/captcha"), params={"lang": "RU"},
                        xsrf=xsrf, referer=referer,
                        extra={"X-Hhtmfrom": "", "X-Hhtmsource": "account_captcha"})
    key = result.get("key")
    if not key:
        raise HHAPIError("Капча HH недоступна")
    image_response = await client.get(urljoin(referer, "/captcha/picture?key=" + quote(str(key))),
                                      headers={"Accept": "image/*", "Referer": referer})
    if not image_response.content:
        raise HHAPIError("Изображение капчи HH не загрузилось")
    text = (await _call(lambda: solve(image_response.content))).strip()
    if not text:
        raise HHAPIError("Капча не введена")
    return {"captchaState": state, "captchaKey": key, "captchaText": text}


async def authorize(config, username, password=None, otp_code=None, captcha_solve=None,
                    http_client=None, pre_delay=LOGIN_PRE_DELAY):
    """Полный вход: страница OAuth -> логин/OTP -> код -> токены в .state.

    password=None означает вход по одноразовому коду (SMS/email), код
    запрашивается через otp_code(). captcha_solve(png_bytes) -> str
    используется при требовании капчи. Возвращает словарь GET /me.
    """
    client = http_client
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=40, follow_redirects=True, headers={
            "User-Agent": DESKTOP_USER_AGENT,
            "Accept": DESKTOP_ACCEPT,
            "Accept-Language": DESKTOP_ACCEPT_LANGUAGE,
        })
    try:
        query = urlencode({
            "client_id": ANDROID_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT,
            "response_type": "code",
            "force_login": "true",
            "role": "applicant",
            "force_role": "true",
            "skip_choose_account": "true",
        })
        response = await client.request("GET", OAUTH_URL + "/authorize?" + query,
                                        follow_redirects=True)
        if response.status_code != 200:
            raise HHAPIError("HH не открыл страницу входа (HTTP %d)" % response.status_code)
        login_url = str(response.url)
        state = parse_initial_state(response.text)
        xsrf = state.get("xsrfToken") or client.cookies.get("_xsrf")
        pubkey = state.get("loginTrustFlagsSecret")
        backurl = parse_qs(urlsplit(login_url).query).get("backurl", [""])[0]
        if not (xsrf and pubkey and backurl):
            raise HHAPIError("На странице входа HH не хватает данных; структура изменилась")
        await asyncio.sleep(pre_delay)
        is_email = "@" in username
        phone = username
        if not is_email:
            digits = re.sub(r"\D", "", username)
            if len(digits) == 11 and digits[0] == "8":
                digits = "7" + digits[1:]
            phone = digits
        captcha = None
        code_sent = False
        entered_code = ""
        for _ in range(MAX_CAPTCHA_ATTEMPTS):
            if password is not None:
                result = await _login_by_password(client, login_url, xsrf, pubkey, phone, password, captcha)
            elif not code_sent:
                result = await _request_otp(client, login_url, xsrf, pubkey, phone, is_email, captcha)
                captcha_state = (result.get("hhcaptcha") or {}).get("captchaState")
                if captcha_state:
                    if not captcha_solve:
                        raise AttentionRequired("HH просит капчу при входе; повторите позже")
                    captcha = await _captcha_solution(client, xsrf, login_url, captcha_state, captcha_solve)
                    continue
                if not result.get("success"):
                    raise HHAPIError("HH не отправил код входа")
                code_sent = True
                entered_code = str(await _call(otp_code)).strip() if otp_code else ""
                if not entered_code:
                    raise HHAPIError("Код входа не введён")
                result = await _confirm_otp(client, login_url, xsrf, pubkey, phone, entered_code)
            else:
                result = await _confirm_otp(client, login_url, xsrf, pubkey, phone, entered_code)
            if password is None and not (result.get("verification") or {}).get("success"):
                error_key = (result.get("error") or {}).get("key", "UNKNOWN_ERROR")
                raise HHAPIError("Ошибка входа по коду: " + str(error_key))
            login_error = result.get("loginError") if isinstance(result, dict) else None
            if login_error:
                raise HHAPIError("Ошибка входа: " + str(login_error.get("code", "unknown")))
            captcha_state = (result.get("hhcaptcha") or {}).get("captchaState") if isinstance(result, dict) else None
            if not captcha_state:
                break
            if not captcha_solve:
                raise AttentionRequired("HH просит капчу при входе; повторите позже")
            captcha = await _captcha_solution(client, xsrf, login_url, captcha_state, captcha_solve)
        else:
            raise HHAPIError("Капча HH не пройдена")
        response = await client.get(backurl, headers={"Referer": login_url}, follow_redirects=False)
        location = response.headers.get("location", "")
        parts = urlsplit(location)
        if parts.scheme != "hhandroid" or parts.netloc != "oauthresponse":
            raise HHAPIError("HH не подтвердил вход (неожиданный ответ OAuth)")
        code = parse_qs(parts.query).get("code", [""])[0]
        if not code:
            raise HHAPIError("HH не выдал код авторизации")
        token_response = await client.post(OAUTH_URL + "/token", data={
            "client_id": ANDROID_CLIENT_ID,
            "client_secret": ANDROID_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        })
        if token_response.status_code != 200:
            raise HHAPIError("HH не выдал токен (HTTP %d)" % token_response.status_code)
        body = token_response.json() if token_response.text else {}
        access = body.get("access_token")
        if not isinstance(access, str) or not access.startswith("USER"):
            raise HHAPIError("HH вернул некорректный токен")
        TokenStore(config.api_token_path).save({
            "access_token": access,
            "refresh_token": body.get("refresh_token"),
            "access_expires_at": time.time() + int(body.get("expires_in") or 0),
        })
        return await HHClient(config, http_client=client, min_interval=0).me()
    finally:
        if owns:
            await client.aclose()
