import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from job_finder.async_store import AsyncStore
from job_finder.config import Config
from job_finder.models import Letter, Vacancy
from job_finder.telegram import Telegram, TelegramError


def message(update_id, text, sender=123, chat=123, kind='private'):
    return {'update_id': update_id, 'message': {'message_id': update_id, 'date': 1700000000,
        'from': {'id': sender, 'is_bot': False, 'first_name': 'User'},
        'chat': {'id': chat, 'type': kind, 'first_name': 'User'}, 'text': text}}


def callback(update_id, data, sender=123, chat=123, kind='private'):
    result = message(update_id, 'Menu', chat=chat, kind=kind)['message']
    result['from'] = {'id': 321, 'is_bot': True, 'first_name': 'Agent'}
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
        self.store = AsyncStore(self.config.db_path)
        await self.store.start()
        self.addAsyncCleanup(self._close_store)
        self.updates = []
        self.requests = []
        self.send_results = []
        self.poll_result = None
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.addAsyncCleanup(self.client.aclose)
        self.telegram = Telegram(self.config, self.store, http_client=self.client)

    async def _close_store(self):
        await self.store.close()

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

    async def reconnect(self):
        await self.store.close()
        self.store = AsyncStore(self.config.db_path)
        await self.store.start()
        self.telegram = Telegram(self.config, self.store, http_client=self.client)

    async def test_only_owner_in_own_private_chat_can_read(self):
        self.updates = [message(1, '/resume', sender=999),
                        message(2, '/letter 42', chat=-123, kind='group'),
                        message(3, '/resume', sender=123, chat=456),
                        message(4, '/history', sender=999, chat=999),
                        callback(5, '/resume', sender=999),
                        callback(6, '/history', chat=-123, kind='supergroup')]
        await self.telegram.poll_once()
        self.assertTrue(await self.store.is_paused())
        self.assertEqual(await self.store.setting('telegram_offset'), 7)
        self.assertEqual(await self.store.pending_messages(), [])
        self.assertFalse(any(method == 'sendMessage' for method, _ in self.requests))

    async def test_read_requests_are_durable_and_duplicate_updates_do_not_repeat_replies(self):
        self.updates = [message(10, '/status'), message(11, '/help')]
        await self.telegram.poll_once()
        self.assertTrue(await self.store.is_paused())
        self.assertEqual(len(await self.store.pending_messages()), 2)
        await self.reconnect()
        await self.telegram.poll_once()
        self.assertEqual(len(await self.store.pending_messages()), 2)
        self.assertEqual(self.requests[-1][1]['offset'], 12)

    async def test_search_command_creates_round_before_acknowledgement(self):
        self.updates = [message(1, '/search')]
        await self.telegram.poll_once()
        cursor = await self.store._conn.execute("SELECT state FROM round_requests")
        states = [row[0] for row in await cursor.fetchall()]
        await cursor.close()
        self.assertEqual(states, ['queued'])
        self.assertEqual(await self.store.setting('telegram_offset'), 2)
        reply = (await self.store.pending_messages())[0]['text']
        self.assertIn('Поиск запущен', reply)

    async def test_old_owner_callback_is_inert_acknowledged_and_markup_removed_once(self):
        await self.store.set('browser_blocked', True)
        self.updates = [callback(2, '/resume')]
        await self.telegram.poll_once()
        await self.telegram.poll_once()
        self.assertTrue(await self.store.is_paused())
        self.assertTrue(await self.store.setting('browser_blocked'))
        self.assertEqual(await self.store.pending_messages(), [])
        self.assertIn(('answerCallbackQuery', {'callback_query_id': 'callback-2'}), self.requests)
        edits = [data for method, data in self.requests if method == 'editMessageReplyMarkup']
        self.assertEqual(edits, [{'chat_id': 123, 'message_id': 2,
                                 'reply_markup': {'inline_keyboard': []}}])

    async def test_cleanup_never_edits_outsider_group_missing_or_invalid_message(self):
        updates = [callback(1, '/pause', sender=999),
                   callback(2, '/pause', chat=-123, kind='group')]
        for number, change in enumerate(({'message_id': 0}, {'message_id': True},
                                         {'message_id': '42'}, {'from': {'id': 123, 'is_bot': False}},
                                         {'from': {}}, {'date': 0}), start=3):
            item = callback(number, '/pause')
            item['callback_query']['message'].update(change)
            updates.append(item)
        self.updates = updates
        await self.telegram.poll_once()
        self.assertFalse(any(method == 'editMessageReplyMarkup' for method, _ in self.requests))
        self.assertEqual(await self.store.pending_messages(), [])
        self.assertTrue(all(set(data) == {'callback_query_id'} for method, data in self.requests
                            if method == 'answerCallbackQuery'))

    async def test_card_delivery_with_inline_button_and_delivered_transition(self):
        await self.store.discover('hh', Vacancy('42', 'Python разработчик', 'Python', company='Акме'))
        await self.store.make_ready('hh:42', Letter('Письмо', ['python_backend'], [], []), 'подходит')
        await self.store.notify_ready('hh:42', 'Нашлась новая релевантная вакансия: X — Y', None)
        await self.store.pause(False)
        await self.store.set('telegram_next_delivery', 0)
        await self.telegram.flush()
        sent = [data for method, data in self.requests if method == 'sendMessage']
        self.assertEqual(len(sent), 1)
        keyboard = sent[0]['reply_markup']['inline_keyboard']
        self.assertEqual(keyboard[0][0]['text'], 'Посмотреть вакансию и письмо')
        # Короткий callback_data (лимит Telegram — 64 байта): id строки outbox.
        self.assertLessEqual(len(keyboard[0][0]['callback_data']), 64)
        self.assertRegex(keyboard[0][0]['callback_data'], r'^c:\d+$')
        self.assertEqual((await self.store.get('hh:42'))['status'], 'delivered')

    async def test_card_button_short_callback_data_for_long_ids(self):
        # Длинные native_id (WWR slug) не ломают кнопку: callback_data — id outbox.
        long_id = 'wwr:' + 'x' * 80
        await self.store.discover('wwr', Vacancy(long_id.split(':')[1], 'Python', 'Python'))
        await self.store.make_ready(long_id, Letter('Письмо', ['python_backend'], [], []), 'подходит')
        await self.store.notify_ready(long_id, 'Вакансия', None)
        await self.store.pause(False)
        await self.store.set('telegram_next_delivery', 0)
        await self.telegram.flush()
        sent = [data for method, data in self.requests if method == 'sendMessage']
        callback_data = sent[0]['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.assertLessEqual(len(callback_data), 64)
        self.assertRegex(callback_data, r'^c:\d+$')

    async def test_card_callback_sends_card_and_marks_read(self):
        await self.store.discover('hh', Vacancy('42', 'Python разработчик', 'Python', company='Акме'))
        await self.store.decide('hh:42', 'ready', 'подходит')
        await self.store.set_letter('hh:42', Letter('Точное письмо', ['python_backend'], [], []))
        await self.store.save_assessment('hh:42', {
            'score': 85, 'score_reason': 'опыт совпадает', 'score_status': 'ok',
            'advice': [{'text': 'Подчеркнуть опыт', 'grounds': 'вакансия требует Python'}],
            'advice_status': 'ok', 'letter_status': 'ok', 'stamp': {}})
        await self.store.delivered_job('hh:42')
        self.updates = [callback(1, 'card:hh:42')]
        await self.telegram.poll_once()
        self.assertIn(('answerCallbackQuery', {'callback_query_id': 'callback-1'}), self.requests)
        replies = [row['text'] for row in await self.store.pending_messages()]
        self.assertEqual(len(replies), 1)
        for expected in ('Python разработчик', '85/100', 'Точное письмо', 'Рекомендации по резюме',
                         'https://hh.ru/vacancy/42'):
            self.assertIn(expected, replies[0])
        self.assertEqual((await self.store.get('hh:42'))['status'], 'read')

    async def test_card_callback_by_outbox_id(self):
        await self.store.discover('hh', Vacancy('42', 'Python разработчик', 'Python', company='Акме'))
        await self.store.decide('hh:42', 'ready', 'подходит')
        await self.store.set_letter('hh:42', Letter('Точное письмо', ['python_backend'], [], []))
        await self.store.save_assessment('hh:42', {
            'score': 85, 'score_reason': 'опыт совпадает', 'score_status': 'ok',
            'advice': [], 'advice_status': 'ok', 'letter_status': 'ok', 'stamp': {}})
        await self.store.delivered_job('hh:42')
        await self.store.notify_ready('hh:42', 'Нашлась новая релевантная вакансия: X', None)
        cursor = await self.store._conn.execute(
            "SELECT id FROM outbox WHERE kind='card' AND job_id='hh:42'")
        outbox_id = (await cursor.fetchone())[0]
        await cursor.close()
        self.updates = [callback(1, 'c:' + str(outbox_id))]
        await self.telegram.poll_once()
        replies = [row['text'] for row in await self.store.pending_messages() if row['kind'] == 'reply']
        self.assertEqual(len(replies), 1)
        self.assertIn('Точное письмо', replies[0])
        self.assertEqual((await self.store.get('hh:42'))['status'], 'read')

    async def test_skip_keep_unskip_commands_with_compound_ids(self):
        await self.store.discover('hh', Vacancy('900001', 'Уточнить', 'Python'))
        await self.store.decide('hh:900001', 'needs_clarification', 'не подтверждён формат')
        self.updates = [message(1, '/keep hh:900001'), message(2, '/skip hh:900001'),
                        message(3, '/unskip hh:900001'), message(4, '/skip без-id')]
        await self.telegram.poll_once()
        row = await self.store.get('hh:900001')
        self.assertEqual(row['status'], 'found')  # keep → ready, skip → skipped, unskip → found
        self.assertEqual(await self.store.setting('telegram_offset'), 5)
        replies = '\n'.join(item['text'] for item in await self.store.pending_messages())
        self.assertIn('Одобрено', replies)
        self.assertIn('Пропущено вместе с кластером', replies)
        self.assertIn('Пропуск кластера отменён', replies)
        self.assertIn('Укажите ID', replies)

    async def test_letter_command_accepts_compound_id(self):
        await self.store.discover('hh', Vacancy('900002', 'Python dev', 'Python'))
        await self.store.decide('hh:900002', 'ready', 'подходит')
        await self.store.set_letter('hh:900002', Letter('Письмо', ['python_backend'], [], []))
        self.updates = [message(1, '/letter hh:900002')]
        await self.telegram.poll_once()
        replies = [row['text'] for row in await self.store.pending_messages()]
        self.assertIn('Письмо', replies[0])

    async def test_all_stale_callback_actions_are_inert_even_read_requests(self):
        await self.store.discover('hh', Vacancy('42', 'Python', 'Python'))
        await self.store.decide('42', 'rejected', 'Не подходит')
        await self.store.pause(False)
        before = await self.store.get('42')
        actions = ('/search', '/pause', '/resume', '/reconcile', '/requeue 42',
                   '/history', '/letter 42', '/status')
        self.updates = [callback(i, action) for i, action in enumerate(actions, 1)]
        await self.telegram.poll_once()
        self.assertFalse(await self.store.is_paused())
        self.assertEqual(await self.store.get('42'), before)
        self.assertEqual(await self.store.pending_messages(), [])

    async def test_uneditable_old_message_does_not_block_reports_or_repeat_action(self):
        def respond(request):
            if request.url.path.endswith('/editMessageReplyMarkup'):
                self.requests.append(('editMessageReplyMarkup', json.loads(request.content)))
                return httpx.Response(400, json={'ok': False, 'description': 'private-token'})
            return self.handle(request)
        self.updates = [callback(1, '/resume'), message(2, '/status')]
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await Telegram(self.config, self.store, http_client=client).poll_once()
        self.assertTrue(await self.store.is_paused())
        messages = await self.store.pending_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn('Пауза доставки', messages[0]['text'])
        self.assertNotIn('private-token', messages[0]['text'])
        self.assertEqual(await self.store.setting('telegram_offset'), 3)
        self.assertIsNone(await self.store.setting('telegram_next_poll'))
        self.assertEqual(sum(method == 'editMessageReplyMarkup' for method, _ in self.requests), 1)

    async def test_start_help_offer_only_read_commands_and_never_controls(self):
        self.updates = [message(1, '/start'), message(2, '/help')]
        await self.telegram.poll_once()
        for row in await self.store.pending_messages():
            self.assertIn('/status', row['text'])
            self.assertIn('/clarifications', row['text'])
            for command in ('/pause', '/resume', '/requeue', '/reconcile', '/queue', '/settings'):
                self.assertNotIn(command, row['text'])

    async def test_command_views_include_exact_letter_claims_and_preparation(self):
        await self.store.discover('hh', Vacancy('42', 'Python engineer', 'Python', company='Company'))
        await self.store.save_letter('42', Letter('Exact letter', ['python_backend'], ['Added claim'], ['Practice task']))
        await self.store.decide('42', 'ready', 'Eligible')
        self.updates = [message(1, '/letter 42'), message(2, '/status'), message(3, '/history'),
                        message(4, '/status'), message(5, '/start'), message(6, '/help')]
        await self.telegram.poll_once()
        texts = '\n'.join(row['text'] for row in await self.store.pending_messages())
        for expected in ('Exact letter', 'Added claim', 'Practice task', 'python_backend',
                         'https://hh.ru/vacancy/42', '137924141', '/status'):
            self.assertIn(expected, texts)
        self.assertNotIn('private-token', texts)

    async def test_retired_pause_and_bad_letter_arguments_are_safe(self):
        await self.store.pause(False)
        self.updates = [message(1, '/pause'), message(2, '/letter ../.env'),
                        message(3, '/letter 999'), message(4, '/wat')]
        await self.telegram.poll_once()
        self.assertFalse(await self.store.is_paused())
        self.assertEqual(await self.store.setting('telegram_offset'), 5)
        self.assertEqual(len(await self.store.pending_messages()), 4)

    async def test_clarifications_list_filters_ready_jobs_and_status_only_counts_them(self):
        await self.store.discover('hh', Vacancy('42', 'Уточнить валюту', 'Python'))
        await self.store.decide('42', 'needs_clarification', 'Неизвестный курс валюты')
        await self.store.discover('hh', Vacancy('43', 'Готовая вакансия', 'Python'))
        await self.store.decide('43', 'ready', 'Подходит')
        self.updates = [message(1, '/clarifications'), message(2, '/status')]
        await self.telegram.poll_once()
        clarification, queue = [row['text'] for row in await self.store.pending_messages()]
        for expected in ('Уточнить валюту', 'Неизвестный курс валюты', 'https://hh.ru/vacancy/42', 'Всего: 1'):
            self.assertIn(expected, clarification)
        self.assertNotIn('Готовая вакансия', clarification)
        self.assertNotIn('Уточнить валюту', queue)
        self.assertIn('готова 1', queue)

    async def test_clarifications_pages_cover_every_entry_once(self):
        for number in range(200, 225):
            await self.store.discover('hh', Vacancy(str(number), 'Уточнение ' + str(number), 'Python'))
            await self.store.decide(str(number), 'needs_clarification', 'Нужно проверить условия')
        self.updates = [message(1, '/clarifications'), message(2, '/clarifications 2')]
        await self.telegram.poll_once()
        first, second = [row['text'] for row in await self.store.pending_messages()]
        self.assertEqual(first.count('https://hh.ru/vacancy/'), 20)
        self.assertEqual(second.count('https://hh.ru/vacancy/'), 5)
        self.assertIn('/clarifications 2', first)
        self.assertNotIn('/clarifications 3', second)
        self.assertIn('Всего: 25', first)
        for number in range(200, 225):
            url = 'https://hh.ru/vacancy/' + str(number)
            self.assertEqual((first + second).count(url), 1)

    async def test_clarifications_empty_list_and_invalid_pages_are_explained(self):
        self.updates = [message(1, '/clarifications')]
        await self.telegram.poll_once()
        messages = await self.store.pending_messages()
        self.assertIn('Уточнений пока нет', messages[0]['text'])
        for argument in ('0', '-1', 'abc', '2', '1 extra', '9' * 5000):
            with self.subTest(argument=argument[:20]):
                action, response = await self.telegram._response('/clarifications ' + argument)
                self.assertEqual(action, 'clarifications')
                self.assertIn('/clarifications', response)
                self.assertNotIn('Команды:', response)

    async def test_clarifications_never_disclose_to_other_users_or_groups(self):
        await self.store.discover('hh', Vacancy('42', 'Личная вакансия', 'Python'))
        await self.store.decide('42', 'needs_clarification', 'Личные условия')
        self.updates = [message(1, '/clarifications', sender=999, chat=999),
                        callback(2, '/clarifications', chat=-123, kind='group')]
        await self.telegram.poll_once()
        self.assertEqual(await self.store.pending_messages(), [])

    async def test_status_explains_attention_counts_and_sources_without_mutation(self):
        await self.store.set('browser_blocked', True)
        await self.store.set('attention_reason', 'Нужно заново войти в HH')
        await self.store.set('search_report:hh', {'ids': ['42', '43'], 'incomplete': True,
                                                  'notes': ['Ограничена выдача']})
        self.updates = [message(1, '/status')]
        await self.telegram.poll_once()
        response = (await self.store.pending_messages())[0]['text']
        self.assertIn('автоотклик удалён', response)
        self.assertIn('Нужно заново войти в HH', response)
        self.assertIn('hh: вакансий 2', response)
        self.assertIn('охват неполный', response)
        self.assertIn('Ограничена выдача', response)
        self.assertTrue(await self.store.is_paused())
        self.assertTrue(await self.store.setting('browser_blocked'))
        await self.store.set('browser_blocked', False)
        _, unblocked = await self.telegram._response('/status')
        self.assertNotIn('Нужно заново войти в HH', unblocked)

    async def test_status_explains_delivery_pause_and_attention_block(self):
        await self.store.discover('hh', Vacancy('42', 'Python', 'Python'))
        before = await self.store.get('42')
        self.updates = [message(1, '/status')]
        await self.telegram.poll_once()
        response = (await self.store.pending_messages())[0]['text']
        self.assertIn('Скрипт работает', response)
        self.assertIn('Пауза доставки: да', response)
        self.assertIn('Доставка уведомлений приостановлена', response)
        self.assertEqual(await self.store.get('42'), before)
        self.assertTrue(await self.store.is_paused())
        await self.store.set('browser_blocked', True)
        await self.store.set('attention_reason', 'Нужно заново войти в HH')
        _, blocked = await self.telegram._response('/status')
        self.assertIn('Нужно заново войти в HH', blocked)
        self.assertIn('доступ', blocked.lower())

    async def test_delivery_only_sends_replies_and_legacy_success(self):
        await self.store.enqueue('Автоматическое событие — не отправлять')
        await self.store.enqueue('Ответ по запросу', kind='reply')
        await self.store.enqueue('Legacy подтверждение отклика', kind='success')
        await self.store.pause(False)
        await self.store.discover('hh', Vacancy('43', 'Уточнить формат работы', 'Python'))
        await self.store.decide('43', 'needs_clarification', 'Не подтверждена удалённая работа')
        self.updates = [message(1, '/clarifications')]
        await self.telegram.poll_once()
        for _ in range(4):
            await self.store.set('telegram_next_delivery', 0)
            await self.telegram.flush()
        sent = [data['text'] for method, data in self.requests if method == 'sendMessage']
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0], 'Ответ по запросу')
        self.assertEqual(sent[1], 'Legacy подтверждение отклика')
        self.assertIn('Уточнить формат работы', sent[2])
        self.assertNotIn('Автоматическое событие', '\n'.join(sent))
        self.assertTrue(all(data['chat_id'] == 123 and 'reply_markup' not in data
                            for method, data in self.requests if method == 'sendMessage'))

    async def test_delivery_pause_holds_messages_in_outbox(self):
        await self.store.enqueue('Сообщение на паузе', kind='reply')
        await self.telegram.flush()
        self.assertFalse(any(method == 'sendMessage' for method, _ in self.requests))
        self.assertEqual(len(await self.store.pending_messages()), 1)
        await self.store.pause(False)
        await self.store.set('telegram_next_delivery', 0)
        await self.telegram.flush()
        self.assertTrue(any(method == 'sendMessage' for method, _ in self.requests))
        self.assertEqual(await self.store.pending_messages(), [])

    async def test_all_retired_commands_leave_work_state_and_jobs_unchanged(self):
        await self.store.discover('hh', Vacancy('42', 'Python', 'Python'))
        await self.store.decide('42', 'rejected', 'Не подходит')
        await self.store.pause(False)
        await self.store.set('browser_blocked', True)
        before = await self.store.get('42')
        commands = ('/pause', '/resume', '/reconcile', '/requeue 42', '/queue', '/settings')
        self.updates = [message(i, command) for i, command in enumerate(commands, 1)]
        await self.telegram.poll_once()
        self.assertFalse(await self.store.is_paused())
        self.assertTrue(await self.store.setting('browser_blocked'))
        self.assertEqual(await self.store.get('42'), before)
        for reply in await self.store.pending_messages():
            self.assertIn('/status', reply['text'])

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
        await self.store.enqueue(text, kind='reply')
        await self.store.pause(False)
        self.send_results = [httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}}),
                             httpx.Response(503, json={'ok': False, 'description': 'secret-url'})]
        with patch('job_finder.telegram.time.time', return_value=1000):
            await self.telegram.flush()
            await self.telegram.flush()
        sends = [data for method, data in self.requests if method == 'sendMessage']
        self.assertEqual(len(sends), 1)
        self.assertLess(len(sends[0]['text'].encode('utf-16-le')) // 2, 4096)
        with patch('job_finder.telegram.time.time', return_value=1002):
            with self.assertRaises(TelegramError):
                await self.telegram.flush()
        await self.reconnect()
        with patch('job_finder.telegram.time.time', return_value=2000):
            await self.telegram.flush()
        with patch('job_finder.telegram.time.time', return_value=2002):
            await self.telegram.flush()
        sends = [data for method, data in self.requests if method == 'sendMessage']
        self.assertEqual(sends[1]['text'], sends[2]['text'])
        self.assertNotEqual(sends[0]['text'], sends[2]['text'])
        self.assertEqual(sends[0]['text'] + sends[2]['text'] + sends[3]['text'], text)
        self.assertEqual(await self.store.pending_messages(), [])
        self.assertTrue(all(data['chat_id'] == 123 and 'parse_mode' not in data for data in sends))

    async def test_poll_429_persists_bounded_retry_and_never_throws_secret_description(self):
        self.poll_result = httpx.Response(429, json={'ok': False, 'error_code': 429,
            'description': 'private-token', 'parameters': {'retry_after': 999999}})
        with patch('job_finder.telegram.time.time', return_value=1000):
            await self.telegram.poll_once()
        self.assertEqual(await self.store.setting('telegram_next_poll'), 4600)
        with patch('job_finder.telegram.time.time', return_value=1001):
            await self.telegram.poll_once()
        self.assertEqual(len(self.requests), 1)

    async def test_delivery_429_retries_same_chunk_after_delay(self):
        await self.store.enqueue('Unsent letter', kind='reply')
        await self.store.pause(False)
        self.send_results = [httpx.Response(200, json={'ok': False, 'error_code': 429,
            'parameters': {'retry_after': 10}})]
        with patch('job_finder.telegram.time.time', return_value=1000):
            await self.telegram.flush()
        with patch('job_finder.telegram.time.time', return_value=1009):
            await self.telegram.flush()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(await self.store.pending_messages()), 1)
        with patch('job_finder.telegram.time.time', return_value=1010):
            await self.telegram.flush()
        self.assertEqual(await self.store.pending_messages(), [])

    async def test_history_shows_latest_twenty_entries(self):
        for number in range(200, 225):
            await self.store.discover('hh', Vacancy(str(number), 'History vacancy ' + str(number), 'Python'))
            await self.store.decide(str(number), 'rejected', 'Отсеяна правилами')
            await self.store._conn.execute('UPDATE jobs SET created=? WHERE id=?',
                                           ('2027-01-01T00:00:' + str(number - 200).zfill(2), 'hh:' + str(number)))
        await self.store._conn.commit()
        self.updates = [message(1, '/history')]
        await self.telegram.poll_once()
        response = (await self.store.pending_messages())[0]['text']
        self.assertIn('History vacancy 224', response)
        self.assertNotIn('History vacancy 200', response)

    async def test_null_success_result_is_backed_off_as_protocol_failure(self):
        self.poll_result = httpx.Response(200, json={'ok': True, 'result': None})
        with patch('job_finder.telegram.time.time', return_value=1000):
            with self.assertRaises(TelegramError):
                await self.telegram.poll_once()
            await self.telegram.poll_once()
        self.assertEqual(len(self.requests), 1)
        self.assertGreater(await self.store.setting('telegram_next_poll'), 1000)

    async def test_callback_429_stops_more_acks_without_dispatching_old_commands(self):
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
            with patch('job_finder.telegram.time.time', return_value=1000):
                await Telegram(self.config, self.store, http_client=client).poll_once()
        self.assertEqual(calls, ['getUpdates', 'answerCallbackQuery'])
        self.assertEqual(await self.store.setting('telegram_offset'), 3)

    async def test_transport_errors_are_sanitized_and_leave_offset_untouched(self):
        async def fail(request):
            raise httpx.ConnectError('secret at ' + str(request.url), request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            telegram = Telegram(self.config, self.store, http_client=client)
            with self.assertRaises(TelegramError) as raised:
                await telegram.poll_once()
            self.assertNotIn('secret', str(raised.exception))
            self.assertNotIn('private-token', str(raised.exception))
            self.assertIsNone(await self.store.setting('telegram_offset'))
