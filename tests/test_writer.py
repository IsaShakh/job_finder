import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from job_finder.config import Config
from job_finder.models import Letter, WriterResult
from job_finder.writer import PROMPT_VERSION, Writer, WriterError
from tests.test_rules import vacancy


def envelope(output, usage=None, model="gpt-4o-mini", finish="stop", content=None):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content if content is not None else json.dumps(output)},
                     "finish_reason": finish}],
        "usage": usage if usage is not None else {"prompt_tokens": 100, "completion_tokens": 50},
        "model": model,
    })


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ, {}, clear=True):
            self.config = Config.load(Path(self.temp.name))
        self.config = replace(self.config, llm_api_key="test-key", llm_model="gpt-4o-mini")
        self.vacancy = vacancy(description="Работа с Python и FastAPI", requirements="FastAPI, Kubernetes")

    def client(self, responder):
        return httpx.AsyncClient(transport=httpx.MockTransport(responder))

    def valid_output(self, **changes):
        writer = Writer(self.config)
        allowed = writer._claims(self.vacancy, 'ru')
        output = {"score": 80, "score_reason": "опыт и стек совпадают",
                  "letter": ("Здравствуйте! Откликаюсь на вакансию Python-разработчика. "
                             "Я Python backend-разработчик, разрабатывал сервисы на FastAPI. "
                             "Готов обсудить задачи команды."),
                  "added_claims": [], "preparation": [],
                  "advice": [{"text": "Подчеркнуть опыт с FastAPI",
                              "grounds": "вакансия требует FastAPI; есть опыт из профиля"}]}
        if changes.get("with_claim"):
            claim = next(iter(allowed))
            output["added_claims"] = [claim]
            output["preparation"] = [allowed[claim]]
            changes.pop("with_claim")
        output.update(changes)
        return output

    async def test_fallback_when_llm_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.load(Path(self.temp.name))
        result = await Writer(config).generate(self.vacancy)
        self.assertEqual(result.score_status, "unavailable")
        self.assertEqual(result.score_reason, "оценка недоступна")
        self.assertEqual(result.letter_status, "ok")
        self.assertIsInstance(result.letter, Letter)
        self.assertEqual(result.letter.fact_ids[0], "python_backend")
        self.assertEqual(result.advice, [])

    async def test_full_result_all_parts_valid(self):
        output = self.valid_output()
        async with self.client(lambda request: envelope(output)) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertEqual((result.score, result.score_status), (80, "ok"))
        self.assertEqual(result.score_reason, "опыт и стек совпадают")
        self.assertEqual(result.letter_status, "ok")
        # Письмо модели попадает в карточку как есть (writer v3).
        self.assertEqual(result.letter.text, output["letter"])
        self.assertEqual(result.letter.fact_ids, [])
        self.assertIn("Здравствуйте", result.letter.text)
        self.assertEqual(len(result.advice), 1)
        self.assertEqual(result.advice[0].grounds, "вакансия требует FastAPI; есть опыт из профиля")
        self.assertEqual(result.usage["prompt_tokens"], 100)

    async def test_claim_in_letter_is_bounded_and_relevant(self):
        writer = Writer(self.config)
        allowed = writer._claims(self.vacancy, 'ru')
        claim = next(iter(allowed))
        output = self.valid_output(with_claim=True)
        async with self.client(lambda request: envelope(output)) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertEqual(result.letter_status, "ok")
        self.assertEqual(result.letter.added_claims, [claim])
        self.assertEqual(result.letter.preparation, [allowed[claim]])

    async def test_score_survives_invalid_letter_parts(self):
        # R10: невалидное письмо не выбрасывает корректную оценку.
        output = self.valid_output(letter=None)
        async with self.client(lambda request: envelope(output)) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertEqual((result.score, result.score_status), (80, "ok"))
        self.assertIsNone(result.letter)
        self.assertEqual(result.letter_status, "failed")
        self.assertEqual(result.advice_status, "ok")

    async def test_advice_requires_grounds(self):
        output = self.valid_output(advice=[{"text": "совет без основания"}])
        async with self.client(lambda request: envelope(output)) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertEqual(result.advice_status, "failed")
        self.assertEqual(result.advice, [])
        self.assertEqual(result.score_status, "ok")
        self.assertEqual(result.letter_status, "ok")

    async def test_score_out_of_range_is_failed(self):
        output = self.valid_output(score=150)
        async with self.client(lambda request: envelope(output)) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertEqual(result.score_status, "failed")
        self.assertIn("недоступна", result.score_reason)
        self.assertEqual(result.letter_status, "ok")

    async def test_claims_not_in_allowlist_fail(self):
        # Утверждения только из allowlist; вакансия не может ввести свои.
        output = self.valid_output(added_claims=["внедрил_python_в_пятницу"],
                                   preparation=["подготовка"])
        async with self.client(lambda request: envelope(output)) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertIsNone(result.letter)
        self.assertEqual(result.letter_status, "failed")

    async def test_letter_with_url_is_rejected(self):
        output = self.valid_output(letter="Здравствуйте! Пишу на https://spam.example/ про себя.")
        async with self.client(lambda request: envelope(output)) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertIsNone(result.letter)
        self.assertEqual(result.letter_status, "failed")
        self.assertEqual(result.score_status, "ok")

    async def test_letter_language_must_match_vacancy(self):
        english = "Hello! I am a Python backend developer with FastAPI experience."
        output = self.valid_output(letter=english)
        async with self.client(lambda request: envelope(output)) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertIsNone(result.letter)
        self.assertEqual(result.letter_status, "failed")

    async def test_letter_too_long_is_rejected(self):
        output = self.valid_output(letter="Здравствуйте! " + "текст " * 800)
        async with self.client(lambda request: envelope(output)) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertIsNone(result.letter)
        self.assertEqual(result.letter_status, "failed")

    async def test_retries_on_invalid_json(self):
        calls = []
        def responder(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(200, json={"choices": [
                    {"message": {"content": "не json"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
            return envelope(self.valid_output())
        async with self.client(responder) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.score_status, "ok")

    async def test_retries_exhausted_keep_fallback_letter(self):
        def responder(request):
            return httpx.Response(200, json={"choices": [
                {"message": {"content": "не json"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        async with self.client(responder) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertEqual(result.score_status, "failed")
        self.assertIn("недоступна", result.score_reason)
        # Письмо из подтверждённых фактов остаётся; карточка не блокируется.
        self.assertEqual(result.letter_status, "ok")
        self.assertIsInstance(result.letter, Letter)

    async def test_http_errors_are_sanitized(self):
        def responder(request):
            return httpx.Response(500, json={"message": "secret-key-value"})
        async with self.client(responder) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertNotIn("secret", result.error)
        self.assertEqual(result.letter_status, "ok")

    async def test_english_vacancy_gets_english_letter(self):
        english = vacancy(title="Python engineer", description="We build APIs with Python",
                          requirements="FastAPI")
        output = self.valid_output(letter="Hello! I am a Python backend developer and "
                                          "I have built services with FastAPI. I would be "
                                          "glad to discuss your team’s tasks.")
        async with self.client(lambda request: envelope(output)) as client:
            result = await Writer(self.config, http_client=client).generate(english)
        self.assertEqual(result.letter_status, "ok")
        self.assertEqual(result.letter.text, output["letter"])

    async def test_cost_computation(self):
        usage = {"model": "gpt-4o-mini", "prompt_tokens": 1000, "completion_tokens": 2000}
        self.assertAlmostEqual(Writer._cost(usage), (1000 * 0.15 + 2000 * 0.60) / 1_000_000)
        self.assertIsNone(Writer._cost({"model": "unknown-model", "prompt_tokens": 10,
                                        "completion_tokens": 10}))
        self.assertIsNone(Writer._cost(None))

    async def test_envelope_validation_fails_with_sanitized_error(self):
        def responder(request):
            return httpx.Response(200, json={"choices": []})
        async with self.client(responder) as client:
            result = await Writer(self.config, http_client=client).generate(self.vacancy)
        self.assertEqual(result.score_status, "failed")
        self.assertNotIn("choices", result.error)
