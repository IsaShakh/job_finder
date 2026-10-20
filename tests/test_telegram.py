import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from hh_agent.config import Config
from hh_agent.models import Letter, Vacancy
from hh_agent.store import Store
from hh_agent.telegram import Telegram, TelegramError


def message(update_id, text, sender=123, chat=123, kind='private'):
    return {'update_id': update_id, 'message': {'message_id': update_id, 'date': 1700000000,
        'from': {'id': sender, 'is_bot': False, 'first_name': 'User'},
        'chat': {'id': chat, 'type': kind, 'first_name': 'User'}, 'text': text}}


def callback(update_id, data, sender=123, chat=123, kind='private'):
    result = message(update_id, 'Menu', chat=chat, kind=kind)['message']
    return {'update_id': update_id, 'callback_query': {'id': 'callback-' + str(update_id),
        'from': {'id': sender, 'is_bot': False, 'first_name': 'User'},
        'message': result, 'chat_instance': 'instance', 'data': data}}


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with patch.dict(os.environ, {}, clear=True):
            self.config = replace(Config.load(Path(self.directory.name)),
                                  owner_id=123, telegram_token='123:private-token')
        self.store = Store(self.config.db_path)
        self.addCleanup(lambda: self.store.close())
        self.updates = []
        self.requests = []
        self.send_results = []
        self.poll_result = None
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.addAsyncCleanup(self.client.aclose)
        self.telegram = Telegram(self.config, self.store, http_client=self.client)

    def handle(self, request):
        method = request.url.path.rsplit('/', 1)[-1]
        data = json.loads(request.content)
        self.requests.append((method, data))
        if method == 'getUpdates':
            if self.poll_result is not None:
                return self.poll_result
            return httpx.Response(200, json={'ok': True, 'result': self.updates})
        if method == 'sendMessage' and self.send_results:
            return self.send_results.pop(0)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})

    async def test_only_owner_in_own_private_chat_can_control_or_read(self):
        self.updates = [message(1, '/resume', sender=999),
                        message(2, '/letter 42', chat=-123, kind='group'),
                        message(3, '/resume', sender=123, chat=456),
                        message(4, '/history', sender=999, chat=999),
                        callback(5, '/resume', sender=999),
                        callback(6, '/history', chat=-123, kind='supergroup')]
        await self.telegram.poll_once()
        self.assertTrue(self.store.is_paused())
        self.assertEqual(self.store.setting('telegram_offset'), 7)
        self.assertEqual(self.store.pending_messages(), [])
        self.assertFalse(any(method == 'sendMessage' for method, _ in self.requests))

    async def test_commands_are_durable_and_duplicate_updates_do_not_repeat_effects(self):
        self.updates = [message(10, '/resume'), message(11, '/search')]
        await self.telegram.poll_once()
        self.assertFalse(self.store.is_paused())
        self.assertTrue(self.store.setting('search_requested'))
        self.assertFalse(self.config.live)
        self.assertEqual(len(self.store.pending_messages()), 2)
        self.store.set('search_requested', False)
        self.store.pause()
        self.store.close()
        self.store = Store(self.config.db_path)
        self.telegram = Telegram(self.config, self.store, http_client=self.client)
        await self.telegram.poll_once()
        self.assertTrue(self.store.is_paused())
        self.assertFalse(self.store.setting('search_requested'))
        self.assertEqual(len(self.store.pending_messages()), 2)
        self.assertEqual(self.requests[-1][1]['offset'], 12)

    async def test_owner_callback_is_acknowledged_and_handled_once(self):
        self.updates = [callback(2, '/resume')]
        await self.telegram.poll_once()
        await self.telegram.poll_once()
        self.assertFalse(self.store.is_paused())
        self.assertEqual(len(self.store.pending_messages()), 1)
        self.assertIn(('answerCallbackQuery', {'callback_query_id': 'callback-2'}), self.requests)

    async def test_command_views_include_exact_letter_claims_and_preparation(self):
        self.store.discover(Vacancy('42', 'Python engineer', 'Python', company='Company'))
        self.store.save_letter('42', Letter('Exact letter', ['python_backend'], ['Added claim'], ['Practice task']))
        self.store.decide('42', 'ready', 'Eligible')
        self.updates = [message(1, '/letter 42'), message(2, '/queue'), message(3, '/history'),
                        message(4, '/settings'), message(5, '/reconcile'), message(6, '/help')]
        await self.telegram.poll_once()
        texts = '\n'.join(row['text'] for row in self.store.pending_messages())
        for expected in ('Exact letter', 'Added claim', 'Practice task', 'python_backend',
                         'https://hh.ru/vacancy/42', '137924141', '/pause'):
            self.assertIn(expected, texts)
        self.assertTrue(self.store.setting('reconcile_requested'))
        self.assertNotIn('private-token', texts)

    async def test_pause_and_bad_letter_arguments_are_safe(self):
        self.store.pause(False)
        self.updates = [message(1, '/pause'), message(2, '/letter ../.env'),
                        message(3, '/letter 999'), message(4, '/wat')]
        await self.telegram.poll_once()
        self.assertTrue(self.store.is_paused())
        self.assertEqual(self.store.setting('telegram_offset'), 5)
        self.assertEqual(len(self.store.pending_messages()), 4)

    async def test_requeue_passes_only_safe_states_to_transactional_store_command(self):
        for number, status in enumerate(('rejected', 'needs_clarification', 'error', 'closed',
                                         'ready', 'sending', 'unknown', 'sent'), start=50):
            self.store.discover(Vacancy(str(number), 'Python', 'Python'))
            self.store.decide(str(number), status, 'Existing decision')
        self.updates = [message(number, '/requeue ' + str(number)) for number in range(50, 58)]
        self.updates += [message(58, '/requeue ../.env'), message(59, '/requeue 999')]
        await self.telegram.poll_once()
        actions = dict(self.store.conn.execute('SELECT update_id,command FROM inbox').fetchall())
        self.assertEqual([actions[key] for key in range(50, 54)],
                         ['requeue:50', 'requeue:51', 'requeue:52', 'requeue:53'])
        self.assertTrue(all(not actions[key].startswith('requeue:') for key in range(54, 60)))
        for number, expected in ((54, 'ready'), (55, 'sending'), (56, 'unknown'), (57, 'sent')):
            self.assertEqual(self.store.get(str(number))['status'], expected)

    async def test_long_poll_is_async_and_uses_bounded_timeout(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def delayed(request):
            data = json.loads(request.content)
            self.assertEqual(data['timeout'], 30)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={'ok': True, 'result': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(delayed)) as client:
            telegram = Telegram(self.config, self.store, http_client=client)
            task = asyncio.create_task(telegram.poll_once())
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(task.done())
            release.set()
            await task

    async def test_delivery_splits_unicode_persists_chunk_progress_and_rate_limits(self):
        text = '🙂' * 3000 + '\n' + 'я' * 3000
        self.store.enqueue(text)
        self.send_results = [httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}}),
                             httpx.Response(503, json={'ok': False, 'description': 'secret-url'})]
        with patch('hh_agent.telegram.time.time', return_value=1000):
            await self.telegram.flush()
            await self.telegram.flush()
        sends = [data for method, data in self.requests if method == 'sendMessage']
        self.assertEqual(len(sends), 1)
        self.assertLess(len(sends[0]['text'].encode('utf-16-le')) // 2, 4096)
        with patch('hh_agent.telegram.time.time', return_value=1002):
            with self.assertRaises(TelegramError):
                await self.telegram.flush()
        self.store.close()
        self.store = Store(self.config.db_path)
        self.telegram = Telegram(self.config, self.store, http_client=self.client)
        with patch('hh_agent.telegram.time.time', return_value=2000):
            await self.telegram.flush()
        with patch('hh_agent.telegram.time.time', return_value=2002):
            await self.telegram.flush()
        sends = [data for method, data in self.requests if method == 'sendMessage']
        self.assertEqual(sends[1]['text'], sends[2]['text'])
        self.assertNotEqual(sends[0]['text'], sends[2]['text'])
        self.assertEqual(sends[0]['text'] + sends[2]['text'] + sends[3]['text'], text)
        self.assertEqual(self.store.pending_messages(), [])
        self.assertTrue(all(data['chat_id'] == 123 and 'parse_mode' not in data for data in sends))

    async def test_poll_429_persists_bounded_retry_and_never_throws_secret_description(self):
        self.poll_result = httpx.Response(429, json={'ok': False, 'error_code': 429,
            'description': 'private-token', 'parameters': {'retry_after': 999999}})
        with patch('hh_agent.telegram.time.time', return_value=1000):
            await self.telegram.poll_once()
        self.assertEqual(self.store.setting('telegram_next_poll'), 4600)
        with patch('hh_agent.telegram.time.time', return_value=1001):
            await self.telegram.poll_once()
        self.assertEqual(len(self.requests), 1)

    async def test_delivery_429_retries_same_chunk_after_delay(self):
        self.store.enqueue('Unsent letter')
        self.send_results = [httpx.Response(200, json={'ok': False, 'error_code': 429,
            'parameters': {'retry_after': 10}})]
        with patch('hh_agent.telegram.time.time', return_value=1000):
            await self.telegram.flush()
        with patch('hh_agent.telegram.time.time', return_value=1009):
            await self.telegram.flush()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.store.pending_messages()), 1)
        with patch('hh_agent.telegram.time.time', return_value=1010):
            await self.telegram.flush()
        self.assertEqual(self.store.pending_messages(), [])

    async def test_history_shows_latest_twenty_entries(self):
        for number in range(200, 225):
            self.store.discover(Vacancy(str(number), 'History vacancy ' + str(number), 'Python'))
            self.store.decide(str(number), 'sent', 'Confirmed earlier')
            with self.store.conn:
                self.store.conn.execute('UPDATE jobs SET created=? WHERE id=?',
                                        ('2027-01-01T00:00:' + str(number - 200).zfill(2), str(number)))
        self.updates = [message(1, '/history')]
        await self.telegram.poll_once()
        response = self.store.pending_messages()[0]['text']
        self.assertIn('History vacancy 224', response)
        self.assertNotIn('History vacancy 200', response)

    async def test_null_success_result_is_backed_off_as_protocol_failure(self):
        self.poll_result = httpx.Response(200, json={'ok': True, 'result': None})
        with patch('hh_agent.telegram.time.time', return_value=1000):
            with self.assertRaises(TelegramError):
                await self.telegram.poll_once()
            await self.telegram.poll_once()
        self.assertEqual(len(self.requests), 1)
        self.assertGreater(self.store.setting('telegram_next_poll'), 1000)

    async def test_callback_429_stops_more_acks_in_batch_but_preserves_commands(self):
        calls = []
        def respond(request):
            method = request.url.path.rsplit('/', 1)[-1]
            calls.append(method)
            if method == 'getUpdates':
                return httpx.Response(200, json={'ok': True, 'result': [
                    callback(1, '/search'), callback(2, '/reconcile')]})
            return httpx.Response(429, json={'ok': False, 'error_code': 429,
                                            'parameters': {'retry_after': 60}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch('hh_agent.telegram.time.time', return_value=1000):
                await Telegram(self.config, self.store, http_client=client).poll_once()
        self.assertEqual(calls, ['getUpdates', 'answerCallbackQuery'])
        self.assertTrue(self.store.setting('search_requested'))
        self.assertTrue(self.store.setting('reconcile_requested'))
        self.assertEqual(self.store.setting('telegram_offset'), 3)

    async def test_transport_errors_are_sanitized_and_leave_offset_untouched(self):
        async def fail(request):
            raise httpx.ConnectError('secret at ' + str(request.url), request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            telegram = Telegram(self.config, self.store, http_client=client)
            with self.assertRaises(TelegramError) as raised:
                await telegram.poll_once()
            self.assertNotIn('secret', str(raised.exception))
            self.assertNotIn('private-token', str(raised.exception))
            self.assertIsNone(self.store.setting('telegram_offset'))
