import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from job_finder.adapters import REGISTRY, AdapterError, SearchReport, build_adapters
from job_finder.adapters.base import RateLimiter, TokenStore, safe_json
from job_finder.adapters.fake import FakeAdapter
from job_finder.config import Config


class BaseHelperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    async def test_rate_limiter_enforces_min_interval(self):
        limiter = RateLimiter(0.05)
        limiter.mark()
        started = asyncio.get_running_loop().time()
        await limiter.pause()
        self.assertGreaterEqual(asyncio.get_running_loop().time() - started, 0.049)
        limiter.mark()
        started = asyncio.get_running_loop().time()
        await limiter.pause()  # прошло меньше интервала — снова ждём
        self.assertGreaterEqual(asyncio.get_running_loop().time() - started, 0.049)

    async def test_token_store_is_atomic_and_private(self):
        path = self.root / ".state" / "tokens.json"
        store = TokenStore(path)
        store.save({"access_token": "T1", "refresh_token": "R1"})
        self.assertEqual(store.load()["access_token"], "T1")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.with_name(path.name + ".tmp").exists(), False)
        store.clear()
        self.assertEqual(store.load(), {})

    async def test_safe_json_never_raises(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload
            def json(self):
                import json
                return json.loads(self.payload)
        self.assertEqual(safe_json(Response('{"a": 1}')), {"a": 1})
        self.assertEqual(safe_json(Response('not json')), {})
        self.assertEqual(safe_json(Response('[1,2]')), {})


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(__import__("os").environ, {}, clear=True):
            self.config = Config.load(Path(self.temp.name))

    async def test_each_source_is_a_file_plus_a_config_line(self):
        # SOURCES — строка конфига; каждый адаптер — файл в пакете.
        config = replace(self.config, sources=("hh", "fake"))
        adapters = build_adapters(config)
        self.assertEqual([adapter.name for adapter in adapters], ["hh", "fake"])
        for adapter in adapters:
            self.assertIsInstance(adapter, REGISTRY[adapter.name])

    async def test_unknown_source_is_rejected(self):
        config = replace(self.config, sources=("hh", "nope"))
        with self.assertRaises(AdapterError):
            build_adapters(config)


class FakeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(__import__("os").environ, {}, clear=True):
            self.config = Config.load(Path(self.temp.name))
        self.adapter = FakeAdapter(self.config)

    async def test_fake_adapter_matches_source_contract(self):
        report = await self.adapter.search(None, None)
        self.assertIsInstance(report, SearchReport)
        ids = [hit.native_id for hit in report.hits]
        self.assertEqual(ids, ["1001", "1002", "1003"])
        vacancy = await self.adapter.fetch("1001")
        self.assertEqual(vacancy.title, "Python разработчик")
        self.assertEqual(vacancy.salary.minimum, 220000)
        missing = await self.adapter.fetch("999")
        self.assertTrue(missing.closed)
        await self.adapter.close()
        with self.assertRaises(RuntimeError):
            await self.adapter.search(None, None)
