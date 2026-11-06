import io
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from job_finder.__main__ import api_command, main, process_lock, save_env_values
from job_finder.async_store import AsyncStore
from job_finder.config import Config
from job_finder.store import Store
from job_finder.telegram import TelegramError


class CliTests(unittest.TestCase):
    def test_launcher_starts_worker_directly_without_menu_or_telegram_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'scripts').mkdir()
            launcher=root/'scripts/start.command'
            source=Path(__file__).resolve().parents[1]/'scripts/start.command'
            launcher.write_bytes(source.read_bytes())
            (root/'.venv/bin').mkdir(parents=True)
            python=root/'.venv/bin/python'
            python.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$HH_TEST_ARGS_PATH"\n')
            python.chmod(0o700)
            record=root/'args'
            result=subprocess.run(['/bin/zsh',str(launcher)], input='0\n',capture_output=True,
                text=True,timeout=3,env={**os.environ,'HH_TEST_ARGS_PATH':str(record)})
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(record.exists(),'Launcher did not start the worker')
            self.assertEqual(record.read_text().splitlines(),['-m','job_finder','run'])

    def run_configure(self, root, answers, secrets=('', ''), env=None):
        prompts=[]
        answers=iter(answers)
        output=io.StringIO()
        def answer(prompt):
            prompts.append(prompt)
            return next(answers)
        with patch.dict(os.environ, env or {}, clear=True), \
                patch('builtins.input', side_effect=answer), \
                patch('getpass.getpass', side_effect=secrets), \
                redirect_stdout(output), redirect_stderr(output):
            code=main(['--root', str(root), 'configure'])
        return code, output.getvalue()+'\n'.join(prompts)

    def test_configure_repairs_invalid_url_without_revealing_or_losing_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            save_env_values(root/'.env', {'LLM_BASE_URL':'mistaken-private-value',
                'TELEGRAM_BOT_TOKEN':'123:saved-token', 'LLM_API_KEY':'saved-key',
                'CUSTOM':'keep'})
            code, output=self.run_configure(root, ['https://api.openai.com/v1', 'chosen-model'])
            self.assertEqual(code, 0, output)
            with patch.dict(os.environ, {}, clear=True):
                config=Config.load(root)
            self.assertEqual(config.llm_base_url, 'https://api.openai.com/v1')
            self.assertEqual(config.llm_model, 'chosen-model')
            self.assertEqual(config.telegram_token, '123:saved-token')
            self.assertEqual(config.llm_api_key, 'saved-key')
            self.assertEqual(dotenv_values(root/'.env')['CUSTOM'], 'keep')
            for secret in ('mistaken-private-value', 'saved-token', 'saved-key'):
                self.assertNotIn(secret, output)

    def test_configure_does_not_save_any_values_when_new_url_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            path=root/'.env'
            save_env_values(path, {'LLM_BASE_URL':'https://api.openai.com/v1',
                'LLM_API_KEY':'saved-key'})
            before=path.read_bytes()
            code, output=self.run_configure(root, ['mistaken-private-value', 'chosen-model'],
                secrets=('123:new-token', 'new-key'))
            self.assertEqual(code, 1, output)
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn('mistaken-private-value', output)
            self.assertNotIn('new-key', output)

    def test_configure_does_not_claim_repair_when_environment_overrides_url(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            path=root/'.env'
            save_env_values(path, {'LLM_BASE_URL':'https://api.openai.com/v1'})
            before=path.read_bytes()
            code, output=self.run_configure(root, ['https://api.openai.com/v1', 'chosen-model'],
                env={'LLM_BASE_URL':'invalid-environment-value'})
            self.assertEqual(code, 1, output)
            self.assertEqual(path.read_bytes(), before)

    def test_configure_does_not_save_invalid_url_masked_by_valid_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            path=root/'.env'
            save_env_values(path, {'LLM_BASE_URL':'https://api.openai.com/v1'})
            before=path.read_bytes()
            code, output=self.run_configure(root, ['invalid-private-value', 'chosen-model'],
                env={'LLM_BASE_URL':'https://api.openai.com/v1'})
            self.assertEqual(code, 1, output)
            self.assertEqual(path.read_bytes(), before)

    def test_configure_blank_answers_preserve_custom_provider_and_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            path=root/'.env'
            save_env_values(path, {'LLM_BASE_URL':'https://models.example/v1',
                'TELEGRAM_BOT_TOKEN':'123:saved-token', 'LLM_API_KEY':'saved-key',
                'LLM_MODEL':'chosen-model'})
            before=path.read_bytes()
            code, output=self.run_configure(root, ['', ''])
            self.assertEqual(code, 0, output)
            self.assertEqual(path.read_bytes(), before)

    def test_init_is_private_and_does_not_overwrite_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            result=subprocess.run([sys.executable,"-m","job_finder","--root",temp,"init"],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            path=root/".env"
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            path.write_text("CUSTOM=keep\n")
            subprocess.run([sys.executable,"-m","job_finder","--root",temp,"init"],check=True,capture_output=True)
            self.assertEqual(path.read_text(),"CUSTOM=keep\n")

    def test_local_process_lock_prevents_second_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            with process_lock(Path(temp)):
                with self.assertRaises(RuntimeError):
                    with process_lock(Path(temp)):
                        self.fail("second worker acquired lock")

    def test_secrets_written_without_executing_or_interpolating_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/".env"
            save_env_values(path,{"LLM_API_KEY":"value'with${HOME}$(no)"})
            from dotenv import dotenv_values
            self.assertEqual(dotenv_values(path,interpolate=False)["LLM_API_KEY"],"value'with${HOME}$(no)")
            self.assertEqual(path.stat().st_mode&0o777,0o600)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def write_token(self, root):
        (root/'.state').mkdir(exist_ok=True)
        (root/'.state'/'api-token.json').write_text(
            json.dumps({'access_token':'USERT','refresh_token':'R','access_expires_at':9999999999}))

    def fake_config(self, root, with_telegram=True):
        env = {'SOURCES': 'fake'}
        with patch.dict(os.environ, env, clear=True):
            config = Config.load(root)
        if with_telegram:
            config = replace(config, telegram_token='123:test-token', owner_id=123)
        return config

    class OfflineBot:
        async def poll_once(self):
            raise TelegramError('Unavailable')
        async def flush(self):
            raise TelegramError('Unavailable')

    async def request_round(self, config):
        requester = AsyncStore(config.db_path)
        await requester.start()
        try:
            await requester.round_request()
        finally:
            await requester.close()

    async def wait_status(self, config, job_id, status, timeout=5):
        db = Store(config.db_path)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                row = db.get(job_id)
                if row and row['status'] == status:
                    return
                await asyncio.sleep(0.05)
            self.assertEqual((db.get(job_id) or {}).get('status'), status)
        finally:
            db.close()

    async def test_run_processes_search_round_and_prepares(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.fake_config(Path(temp))
            with patch('job_finder.telegram.Telegram', return_value=self.OfflineBot()), \
                    patch.dict(os.environ, {'SOURCES': 'fake'}, clear=True):
                task = asyncio.create_task(api_command(config, 'run'))
                try:
                    await self.request_round(config)
                    await self.wait_status(config, 'fake:1001', 'ready')
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_search_command_runs_round_without_preparation(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.fake_config(Path(temp), with_telegram=False)
            with patch.dict(os.environ, {'SOURCES': 'fake'}, clear=True):
                await api_command(config, 'search')
            db = Store(config.db_path)
            try:
                self.assertEqual(db.get('fake:1001')['status'], 'found')
                self.assertEqual(db.get('fake:1003')['status'], 'found')
                self.assertEqual(len(db.rows(['found'], limit=10)), 3)
                self.assertIsNotNone(db.setting('last_search:fake'))
            finally:
                db.close()

    async def test_doctor_reports_sources_without_network(self):
        from job_finder.__main__ import doctor
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {'SOURCES': 'fake,telegram'}, clear=True):
                config = replace(Config.load(Path(temp)),
                                 telegram_token='123:test-token', owner_id=123)
            output = io.StringIO()
            with redirect_stdout(output):
                await doctor(config)
            text = output.getvalue()
            self.assertIn('fake', text)
            self.assertIn('тестовый источник', text)
            self.assertIn('telegram', text)
            self.assertIn('нет сессии', text)

    def test_setup_logging_creates_rotating_agent_log(self):
        from job_finder.__main__ import setup_logging
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {}, clear=True):
                config = Config.load(Path(temp))
            log = setup_logging(config)
            log.info("тестовая запись лога")
            path = config.state_dir / "agent.log"
            self.assertTrue(path.exists())
            self.assertIn("тестовая запись лога", path.read_text(encoding="utf-8"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_run_without_api_token_fails_with_guidance(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {}, clear=True):
                config = replace(Config.load(Path(temp)),
                                 telegram_token='123:test-token', owner_id=123)
            with self.assertRaises(ValueError) as caught:
                await api_command(config, 'run')
            self.assertIn('api-login', str(caught.exception))

    async def test_telegram_outage_does_not_stop_local_search_or_preparation(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.fake_config(Path(temp))
            with patch('job_finder.telegram.Telegram', return_value=self.OfflineBot()), \
                    patch.dict(os.environ, {'SOURCES': 'fake'}, clear=True):
                task = asyncio.create_task(api_command(config, 'run'))
                try:
                    await self.request_round(config)
                    await self.wait_status(config, 'fake:1001', 'ready')
                    db = Store(config.db_path)
                    try:
                        # Перезапуск снимает паузу доставки; поиск и подготовка идут.
                        self.assertFalse(db.is_paused())
                    finally:
                        db.close()
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
