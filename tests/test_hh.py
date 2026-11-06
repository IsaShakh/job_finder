import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from job_finder.config import Config
from job_finder.adapters.hh import (HHAPIError, HHClient, TokenStore, authorize,
                                    trust_flags, vacancy_from_json)
from job_finder.models import Vacancy

VACANCY_JSON = {
    "id": "123", "name": "Python Developer",
    "description": "<p>Работа с <b>Python</b> и FastAPI.</p>",
    "employer": {"id": "1", "name": "ACME"},
    "salary": {"from": 150000, "to": 250000, "currency": "RUR", "gross": False},
    "work_format": [{"id": "REMOTE"}],
    "has_test": False, "archived": False, "type": {"id": "open"},
    "relations": [], "key_skills": [{"name": "Python"}, {"name": "FastAPI"}],
}


def make_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public_pem


def decrypt_flags(private_key, encoded):
    return json.loads(private_key.decrypt(
        base64.b64decode(encoded),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None)))


def login_html(xsrf, public_pem):
    payload = json.dumps({"xsrfToken": xsrf, "loginTrustFlagsSecret": public_pem,
                          "redirectConfig": {}}).replace('"', "&#34;")
    return ('<html><body><template id="HH-Lux-InitialState">' + payload
            + "</template></body></html>")


class Router:
    """Маршрутизатор MockTransport; обработчики бывают повторяемыми и одноразовыми."""

    def __init__(self):
        self.routes = {}
        self.requests = []

    def add(self, method, path, handler, one_shot=False):
        self.routes.setdefault((method, path), []).append((handler, one_shot))

    def handle(self, request):
        self.requests.append(request)
        handlers = self.routes.get((request.method, request.url.path), [])
        if not handlers:
            raise AssertionError("unexpected request %s %s" % (request.method, request.url))
        handler, one_shot = handlers[0]
        if one_shot:
            handlers.pop(0)
        return handler(request)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config.load(self.root)
        self.router = Router()

    async def asyncTearDown(self):
        self.temp.cleanup()

    def client(self, min_interval=0):
        return HHClient(self.config, http_client=self.router.client(), min_interval=min_interval)

    async def test_config_exposes_api_token_path(self):
        self.assertEqual(self.config.api_token_path,
                         (self.root / ".state" / "api-token.json").resolve())

    async def test_token_store_is_atomic_and_private(self):
        store = TokenStore(self.config.api_token_path)
        store.save({"access_token": "USERT", "refresh_token": "R", "access_expires_at": 1})
        self.assertEqual(store.load()["access_token"], "USERT")
        self.assertEqual(self.config.api_token_path.stat().st_mode & 0o777, 0o600)
        store.clear()
        self.assertEqual(store.load(), {})

    async def test_trust_flags_are_rsa_oaep_with_timestamp(self):
        private, public_pem = make_key()
        flags = trust_flags(public_pem, {"password": {"suggest": True}})
        data = decrypt_flags(private, flags)
        self.assertTrue(data["password"]["suggest"])
        self.assertGreater(data["ts"], 0)

    def test_vacancy_conversion(self):
        vacancy = vacancy_from_json(VACANCY_JSON)
        self.assertEqual(vacancy.id, "123")
        self.assertEqual(vacancy.title, "Python Developer")
        self.assertEqual(vacancy.company, "ACME")
        self.assertEqual(vacancy.description, "Работа с Python и FastAPI.")
        self.assertEqual(vacancy.requirements, "Python, FastAPI")
        self.assertEqual(vacancy.salary.minimum, 150000)
        self.assertEqual(vacancy.salary.maximum, 250000)
        self.assertEqual(vacancy.salary.currency, "RUB")
        self.assertTrue(vacancy.remote)
        self.assertFalse(vacancy.test_required)
        self.assertFalse(vacancy.closed)
        closed = dict(VACANCY_JSON, archived=True)
        self.assertTrue(vacancy_from_json(closed).closed)

    def test_vacancy_conversion_has_no_apply_fields(self):
        # Этап 1b: отклики удалены; relations больше не превращаются
        # в признак «уже откликался».
        with_relation = dict(VACANCY_JSON, relations=["got_rejection"])
        vacancy = vacancy_from_json(with_relation)
        self.assertFalse(hasattr(vacancy, "already_applied"))

    async def test_client_has_no_apply_or_reconcile(self):
        # Этап 1b: активный код откликов удалён из API-клиента.
        client = self.client()
        for name in ("apply", "reconcile", "_negotiation_ids", "_confirm", "_describe_error"):
            self.assertFalse(hasattr(client, name))
        await client.close()

    async def test_search_paginates_dedupes_and_reports(self):
        pages = {0: list(range(1, 101)), 1: list(range(101, 201)), 2: list(range(201, 251))}
        def handler(request):
            page = int(request.url.params.get("page", "0"))
            items = pages.get(page, [])
            return httpx.Response(200, json={"found": 250, "pages": 3,
                                             "items": [{"id": item} for item in items]})
        self.router.add("GET", "/vacancies", handler)
        report = await self.client().search(max_pages=100)
        ids = [hit.native_id for hit in report.hits]
        self.assertEqual(len(ids), 250)
        self.assertEqual(ids, [str(item) for item in range(1, 251)])
        self.assertFalse(report.incomplete)

    async def test_search_splits_experience_when_capped(self):
        def handler(request):
            experience = request.url.params.get("experience")
            if not experience:
                page = int(request.url.params.get("page", "0"))
                items = list(range(1, 101)) if page == 0 else list(range(101, 201))
                return httpx.Response(200, json={"found": 5000, "pages": 2,
                                                 "items": [{"id": item} for item in items]})
            marker = {"noExperience": 1001, "between1And3": 1002,
                      "between3And6": 1003, "moreThan6": 1004}[experience]
            return httpx.Response(200, json={"found": 1, "pages": 1, "items": [{"id": marker}]})
        self.router.add("GET", "/vacancies", handler)
        report = await self.client().search(max_pages=100)
        ids = [hit.native_id for hit in report.hits]
        for item in ("1001", "1002", "1003", "1004"):
            self.assertIn(item, ids)
        self.assertTrue(any("диапазона" in note for note in report.notes))

    async def test_fetch_converts_and_missing_is_closed(self):
        self.router.add("GET", "/vacancies/123", lambda request: httpx.Response(200, json=VACANCY_JSON))
        self.router.add("GET", "/vacancies/999",
                        lambda request: httpx.Response(404, json={"errors": [{"type": "not_found"}]}))
        client = self.client()
        vacancy = await client.fetch("123")
        self.assertIsInstance(vacancy, Vacancy)
        self.assertEqual(vacancy.title, "Python Developer")
        closed = await client.fetch("999")
        self.assertTrue(closed.closed)
        await client.close()

    async def test_expired_token_refreshes_once(self):
        TokenStore(self.config.api_token_path).save(
            {"access_token": "USEROLD", "refresh_token": "REF1", "access_expires_at": 1})
        def me(request):
            if request.headers.get("authorization") == "Bearer USERNEW":
                return httpx.Response(200, json={"id": "7", "first_name": "Иса"})
            return httpx.Response(403, json={"errors": [{"type": "forbidden"}]})
        self.router.add("GET", "/me", me)
        self.router.add("POST", "/oauth/token", lambda request: httpx.Response(
            200, json={"access_token": "USERNEW", "refresh_token": "REF2", "expires_in": 1209600}))
        client = self.client()
        user = await client.me()
        self.assertEqual(user["first_name"], "Иса")
        self.assertEqual(TokenStore(self.config.api_token_path).load()["access_token"], "USERNEW")
        await client.close()

    async def test_errors_are_sanitized(self):
        self.router.add("GET", "/me", lambda request: httpx.Response(
            500, json={"message": "secret USERTOKEN internal stack"}))
        client = self.client()
        with self.assertRaises(HHAPIError) as caught:
            await client.me()
        self.assertNotIn("USERTOKEN", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        await client.close()

    def authorize_router(self, public_pem):
        backurl = "https://hh.ru/oauth/authorize?client_id=x"
        self.router.add("GET", "/oauth/authorize", lambda request: httpx.Response(
            302, headers={"Location": "https://hh.ru/account/login?backurl=" + quote(backurl)}),
            one_shot=True)
        self.router.add("GET", "/account/login", lambda request: httpx.Response(
            200, html=login_html("xsrf1", public_pem)))
        self.router.add("POST", "/oauth/token", lambda request: httpx.Response(
            200, json={"access_token": "USERT1", "refresh_token": "REFR1", "expires_in": 1209600}))
        self.router.add("GET", "/me", lambda request: httpx.Response(
            200, json={"first_name": "Иса", "last_name": "Ш"}))
        return backurl

    def finish_authorize(self, backurl):
        self.router.add("GET", "/oauth/authorize", lambda request: httpx.Response(
            302, headers={"Location": "hhandroid://oauthresponse?code=AUTHCODE"}),
            one_shot=True)

    async def test_authorize_by_password_full_flow(self):
        private, public_pem = make_key()
        backurl = self.authorize_router(public_pem)
        def login(request):
            data = parse_qs(request.content.decode())
            self.assertEqual(data["username"], ["71234567890"])
            self.assertEqual(data["password"], ["secret-pass"])
            flags = decrypt_flags(private, data["loginTrustFlags"][0])
            self.assertTrue(flags["password"]["suggest"])
            return httpx.Response(200, json={})
        self.router.add("POST", "/account/login", login)
        self.finish_authorize(backurl)
        me = await authorize(self.config, "8 123 456-78-90", "secret-pass",
                             http_client=self.router.client(), pre_delay=0)
        self.assertEqual(me["first_name"], "Иса")
        saved = TokenStore(self.config.api_token_path).load()
        self.assertEqual(saved["access_token"], "USERT1")

    async def test_authorize_with_relative_backurl(self):
        # Живой HH в OTP-сценарии может вернуть backurl относительным
        # (/oauth/authorize?...); authorize должен его нормализовать.
        private, public_pem = make_key()
        backurl = "/oauth/authorize?client_id=x"
        self.router.add("GET", "/oauth/authorize", lambda request: httpx.Response(
            302, headers={"Location": "https://hh.ru/account/login?backurl=" + quote(backurl)}),
            one_shot=True)
        self.router.add("GET", "/account/login", lambda request: httpx.Response(
            200, html=login_html("xsrf1", public_pem)))
        self.router.add("POST", "/oauth/token", lambda request: httpx.Response(
            200, json={"access_token": "USERT1", "refresh_token": "REFR1", "expires_in": 1209600}))
        self.router.add("GET", "/me", lambda request: httpx.Response(
            200, json={"first_name": "Иса", "last_name": "Ш"}))
        self.router.add("POST", "/account/otp_generate", lambda request: httpx.Response(
            200, json={"success": True}))
        self.router.add("POST", "/account/login/by_code", lambda request: httpx.Response(
            200, json={"success": True, "verification": {"success": True}}))
        self.finish_authorize(backurl)
        me = await authorize(self.config, "71234567890", None,
                             otp_code=lambda: "1234", http_client=self.router.client(), pre_delay=0)
        self.assertEqual(me["first_name"], "Иса")

    async def test_authorize_by_otp_full_flow(self):
        private, public_pem = make_key()
        backurl = self.authorize_router(public_pem)
        self.router.add("POST", "/account/otp_generate", lambda request: httpx.Response(
            200, json={"success": True}))
        def by_code(request):
            data = parse_qs(request.content.decode())
            self.assertEqual(data["code"], ["1234"])
            return httpx.Response(200, json={"success": True, "verification": {"success": True}})
        self.router.add("POST", "/account/login/by_code", by_code)
        self.finish_authorize(backurl)
        me = await authorize(self.config, "user@example.com", None,
                             otp_code=lambda: "1234", http_client=self.router.client(), pre_delay=0)
        self.assertEqual(me["first_name"], "Иса")

    async def test_authorize_solves_login_captcha(self):
        private, public_pem = make_key()
        backurl = self.authorize_router(public_pem)
        attempts = []
        def login(request):
            data = parse_qs(request.content.decode())
            attempts.append(data)
            if "captchaState" not in data:
                return httpx.Response(200, json={"hhcaptcha": {"captchaState": "ST1"}})
            self.assertEqual(data["captchaState"], ["ST1"])
            self.assertEqual(data["captchaText"], ["abcd"])
            return httpx.Response(200, json={})
        self.router.add("POST", "/account/login", login)
        self.router.add("POST", "/captcha", lambda request: httpx.Response(200, json={"key": "K1"}))
        self.router.add("GET", "/captcha/picture", lambda request: httpx.Response(200, content=b"png-bytes"))
        self.finish_authorize(backurl)
        await authorize(self.config, "71234567890", "secret-pass",
                        captcha_solve=lambda png: "abcd",
                        http_client=self.router.client(), pre_delay=0)
        self.assertEqual(len(attempts), 2)

    async def test_authorize_rejects_unexpected_oauth_response(self):
        private, public_pem = make_key()
        backurl = self.authorize_router(public_pem)
        self.router.add("POST", "/account/login", lambda request: httpx.Response(200, json={}))
        self.router.add("GET", "/oauth/authorize", lambda request: httpx.Response(
            302, headers={"Location": "https://example.com/elsewhere"}), one_shot=True)
        with self.assertRaises(HHAPIError):
            await authorize(self.config, "71234567890", "secret-pass",
                            http_client=self.router.client(), pre_delay=0)
        self.assertFalse(self.config.api_token_path.exists())

    async def test_authorize_failure_does_not_leave_token_file(self):
        private, public_pem = make_key()
        self.authorize_router(public_pem)
        self.router.add("POST", "/account/login", lambda request: httpx.Response(
            200, json={"loginError": {"code": "bad_credentials"}}))
        with self.assertRaises(HHAPIError):
            await authorize(self.config, "71234567890", "wrong",
                            http_client=self.router.client(), pre_delay=0)
        self.assertFalse(self.config.api_token_path.exists())
