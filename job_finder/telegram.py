"""Owner-only Telegram reports with durable updates and notification chunks.

Run poll_once in a task separate from the worker and flush loop: long polling
waits up to 30 seconds. No synchronous sleeps occur here. flush sends at most one
chunk and persists its next allowed delivery time, so callers can poll freely.
Команда /search принимает раунд: строка round_requests сохраняется до
квитирования обновления Telegram (контракт 16.5).

Telegram sendMessage and SQLite cannot share a transaction. A crash after the
remote send succeeds but before local progress commits may duplicate ONE chunk.
"""
import json
import re
import time

import httpx

HELP = ('Команды:\n/search — запустить раунд поиска по всем источникам\n'
        '/status — состояние работы и источников\n'
        '/history — история\n/letter ID — письмо и подготовка\n'
        '/clarifications [страница] — вакансии, требующие уточнения\n/help — помощь\n\n'
        'Работа начинается при запуске скрипта на компьютере. Остановка — закрытием процесса. '
        'Telegram показывает отчёты; откликаетесь вы сами, автоматической отправки нет.')

STATUS_LABELS = {'found': 'найдена', 'read': 'прочитана', 'rejected': 'отсеяна',
    'skipped': 'пропущена', 'needs_clarification': 'нужно уточнение',
    'ready': 'готова', 'delivered': 'доставлена', 'error': 'ошибка', 'closed': 'закрыта'}


class TelegramError(RuntimeError):
    """Sanitized API failure: never contains request URLs, tokens or raw bodies."""


def _object(value):
    return value if isinstance(value, dict) else {}


def _chunks(text):
    """Keep each chunk below 4096 UTF-16 units without breaking code points."""
    result, buffer, units = [], [], 0
    for character in text:
        width = 2 if ord(character) > 0xffff else 1
        if units + width > 4000:
            result.append(''.join(buffer))
            buffer, units = [], 0
        buffer.append(character)
        units += width
    if buffer:
        result.append(''.join(buffer))
    return result


class Telegram:
    def __init__(self, config, store, http_client=None):
        if not config.telegram_token or config.owner_id <= 0:
            raise ValueError('Для Telegram нужны токен и положительный ID владельца')
        self.config = config
        self.store = store
        self.http_client = http_client

    def _authorized(self, sender, chat):
        return (type(sender.get('id')) is int and type(chat.get('id')) is int
                and sender['id'] == self.config.owner_id == chat['id']
                and chat.get('type') == 'private' and not sender.get('is_bot', False))

    async def _response(self, text):
        parts = text.strip().split()
        command = parts[0].split('@', 1)[0].lower() if parts else ''
        if command in ('/pause', '/resume', '/reconcile', '/requeue', '/settings', '/queue'):
            return 'retired', ('Эта команда больше не управляет работой. Telegram показывает отчёты. '
                'Состояние: /status. Поиск — /search. Запуск и остановка — на компьютере.')
        if command == '/search':
            # Строка раунда создаётся в store.command до квитирования обновления.
            return 'search', None
        if command == '/status':
            counts = await self.store.counts()
            blocked = await self.store.setting('browser_blocked', False)
            paused = await self.store.is_paused()
            lines = ['Скрипт работает; доступ к HH заблокирован.' if blocked else 'Скрипт работает.',
                     'Пауза доставки: ' + ('да' if paused else 'нет'),
                     'Всего: ' + ', '.join(STATUS_LABELS.get(key, key) + ' ' + str(value)
                                           for key, value in sorted(counts.items())),
                     'Уведомления: по запросу; автоотклик удалён — откликаетесь вы сами.']
            if blocked:
                lines.append('Нужно действие: ' + str(await self.store.setting('attention_reason', 'Проверьте вход и доступ к HH')))
            if paused:
                lines.append('Доставка уведомлений приостановлена.')
            reported = False
            for source in self.config.sources:
                report = await self.store.setting('search_report:' + source)
                if not isinstance(report, dict):
                    continue
                reported = True
                coverage = 'охват неполный' if report.get('incomplete') else 'выдача обработана'
                lines.append(source + ': вакансий ' + str(len(report.get('ids', []))) + ', ' + coverage)
                lines.extend(str(note) for note in (report.get('notes') or []))
            if not reported:
                lines.append('Результатов поиска пока нет. /search — запустить раунд.')
            return 'status', '\n'.join(lines)
        if command == '/clarifications':
            if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r'[1-9][0-9]{0,8}', parts[1])):
                return 'clarifications', 'Укажите положительный номер страницы: /clarifications 1'
            page = int(parts[1]) if len(parts) == 2 else 1
            counts = await self.store.counts()
            total = counts.get('needs_clarification', 0)
            pages = max(1, (total + 19) // 20)
            if page > pages:
                return 'clarifications', 'Такой страницы нет. Доступно страниц: ' + str(pages) + '. /clarifications 1'
            rows = await self.store.rows(['needs_clarification'], limit=20, offset=(page - 1) * 20)
            lines = ['Уточнения — страница ' + str(page) + ' из ' + str(pages), 'Всего: ' + str(total)]
            for row in rows:
                vacancy = row['vacancy']
                lines.append(str(vacancy.id) + ' — ' + vacancy.title[:200] + '\n'
                             + row['reason'][:500] + '\n' + vacancy.url)
            if not rows:
                lines.append('Уточнений пока нет.')
            if page < pages:
                lines.append('Следующая страница: /clarifications ' + str(page + 1))
            return 'clarifications', '\n\n'.join(lines)
        if command == '/history':
            statuses = ['rejected', 'skipped', 'closed', 'error']
            rows = await self.store.rows(statuses, limit=20, newest=True)
            lines = ['История (до 20 записей)']
            for row in rows:
                vacancy = row['vacancy']
                lines.append(str(vacancy.id) + ' — ' + vacancy.title[:200] + '\n'
                    + STATUS_LABELS.get(row['status'], row['status']) + ': ' + row['reason'][:500]
                    + '\n' + vacancy.url)
            if not rows:
                lines.append('Записей пока нет.')
            counts = await self.store.counts()
            lines.append('Всего: ' + ', '.join(STATUS_LABELS.get(key, key) + ' ' + str(value)
                                               for key, value in sorted(counts.items())))
            return command[1:], '\n\n'.join(lines)
        if command == '/letter':
            if len(parts) != 2 or not re.fullmatch(r'[0-9]+', parts[1]):
                return 'letter', 'Укажите числовой ID: /letter 123456'
            row = await self.store.get(parts[1])
            if row is None or row['letter'] is None:
                return 'letter', 'Сохранённое письмо для этой вакансии отсутствует.'
            letter = row['letter']
            return 'letter', (row['vacancy'].url + '\n\n' + letter.text
                + '\n\nФакты профиля: ' + ', '.join(letter.fact_ids)
                + '\n\nДобавленные утверждения (только для вас):\n'
                + ('\n'.join(letter.added_claims) if letter.added_claims else 'Нет.')
                + '\n\nПодготовка (только для вас):\n'
                + ('\n'.join(letter.preparation) if letter.preparation else 'Не требуется по добавленным утверждениям.'))
        return 'help', HELP

    async def poll_once(self):
        if time.time() < await self.store.setting('telegram_next_poll', 0):
            return 0
        offset = await self.store.setting('telegram_offset', 0)
        result = await self._api('getUpdates', {'offset': offset, 'timeout': 30,
            'allowed_updates': ['message', 'callback_query']}, 'telegram_next_poll', timeout=40)
        if result is None:
            return 0
        if not isinstance(result, list):
            await self.store.set('telegram_next_poll', time.time() + 5)
            raise TelegramError('Telegram вернул некорректный список обновлений')
        updates = sorted((item for item in result if isinstance(item, dict)
                          and type(item.get('update_id')) is int and item['update_id'] >= 0),
                         key=lambda item: item['update_id'])
        processed = 0
        for update in updates:
            callback = _object(update.get('callback_query'))
            message = _object(callback.get('message')) if callback else _object(update.get('message'))
            sender = _object(callback.get('from')) if callback else _object(message.get('from'))
            chat = _object(message.get('chat'))
            authorized = self._authorized(sender, chat)
            # An already-consumed update must not move the durable offset backwards.
            fresh = update['update_id'] >= await self.store.setting('telegram_offset', 0)
            if fresh:
                raw = message.get('text')
                if callback:
                    await self.store.command(update['update_id'], 'retired_callback')
                elif authorized and isinstance(raw, str):
                    action, response = await self._response(raw)
                    processed += bool(await self.store.command(update['update_id'], action, response))
                else:
                    await self.store.command(update['update_id'], 'ignored')
            callback_id = callback.get('id')
            if (isinstance(callback_id, str) and callback_id and len(callback_id) <= 512
                    and time.time() >= await self.store.setting('telegram_next_poll', 0)):
                try:
                    # Empty acknowledgement reveals no owner data, including to outsiders.
                    await self._api('answerCallbackQuery', {'callback_query_id': callback_id},
                                    'telegram_next_poll', timeout=10)
                except TelegramError:
                    # The update and offset are already durable. Do not replay either.
                    pass
            if callback and authorized and fresh:
                await self._remove_old_keyboard(message)
        return processed

    async def _remove_old_keyboard(self, message):
        # Bot API guarantees CallbackQuery.message originated from this bot.
        # Inaccessible messages have no sender and date=0; never probe their IDs.
        author = _object(message.get('from'))
        message_id, date = message.get('message_id'), message.get('date')
        if (author.get('is_bot') is not True or type(author.get('id')) is not int
                or author['id'] <= 0 or type(message_id) is not int or not 0 < message_id <= 2147483647
                or type(date) is not int or date <= 0
                or time.time() < await self.store.setting('telegram_next_poll', 0)
                or time.time() < await self.store.setting('telegram_next_cleanup', 0)):
            return
        try:
            await self._api('editMessageReplyMarkup', {'chat_id': self.config.owner_id,
                'message_id': message_id, 'reply_markup': {'inline_keyboard': []}},
                'telegram_next_cleanup', timeout=10)
        except TelegramError:
            # Old/deleted messages may be uneditable. Their actions stay inert.
            pass

    async def flush(self):
        if time.time() < await self.store.setting('telegram_next_delivery', 0):
            return 0
        # Пауза доставки: сообщения накапливаются в outbox, но не отправляются.
        if await self.store.is_paused():
            return 0
        for message in await self.store.pending_messages():
            chunks = _chunks(message['text'])
            key = 'telegram_chunk_' + str(message['id'])
            index = await self.store.setting(key, 0)
            if type(index) is not int or index < 0 or index > len(chunks):
                raise TelegramError('Нарушен сохранённый прогресс уведомления')
            if index == len(chunks):
                await self.store.delivered(message['id'])
                continue
            payload = {'chat_id': self.config.owner_id, 'text': chunks[index],
                       'link_preview_options': {'is_disabled': True}}
            result = await self._api('sendMessage', payload, 'telegram_next_delivery', timeout=20)
            if result is None:
                return 0
            if type(_object(result).get('message_id')) is not int:
                await self.store.set('telegram_next_delivery', time.time() + 30)
                raise TelegramError('Telegram не подтвердил доставку уведомления')
            # Persist the acknowledgement before moving to the next chunk.
            await self.store.set(key, index + 1)
            await self.store.set('telegram_next_delivery', time.time() + 1.05)
            if index + 1 == len(chunks):
                await self.store.delivered(message['id'])
            return 1
        return 0

    async def _api(self, method, payload, retry_key, timeout):
        try:
            if self.http_client is None:
                async with httpx.AsyncClient(follow_redirects=False) as client:
                    response = await client.post('https://api.telegram.org/bot' + self.config.telegram_token
                        + '/' + method, json=payload, timeout=timeout)
            else:
                response = await self.http_client.post('https://api.telegram.org/bot' + self.config.telegram_token
                    + '/' + method, json=payload, timeout=timeout)
        except httpx.HTTPError:
            await self.store.set(retry_key, time.time() + 30)
            raise TelegramError('Не удалось связаться с Telegram; повтор будет позже') from None
        try:
            body = _object(response.json())
        except (json.JSONDecodeError, ValueError):
            body = {}
        if response.status_code == 429 or body.get('error_code') == 429:
            retry_after = _object(body.get('parameters')).get('retry_after', 30)
            if type(retry_after) is not int:
                retry_after = 30
            await self.store.set(retry_key, time.time() + max(1, min(retry_after, 3600)))
            return None
        if response.status_code != 200 or body.get('ok') is not True or body.get('result') is None:
            await self.store.set(retry_key, time.time() + 30)
            raise TelegramError('Telegram отклонил запрос (HTTP ' + str(response.status_code) + ')')
        return body['result']
