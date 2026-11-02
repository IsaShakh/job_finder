"""Owner-only Telegram reports with durable updates and notification chunks.

Run poll_once in a task separate from the HH worker and flush loop: long polling
waits up to 30 seconds. No synchronous sleeps occur here. flush sends at most one
chunk and persists its next allowed delivery time, so callers can poll freely.

Telegram sendMessage and SQLite cannot share a transaction. A crash after the
remote send succeeds but before local progress commits may duplicate ONE chunk.
Application submission is unrelated to these notification retries.
"""
import json
import re
import time

import httpx

HELP = ('Команды отчётов:\n/status — состояние работы и последнего поиска\n'
        '/history — история\n/letter ID — письмо и подготовка\n'
        '/clarifications [страница] — вакансии, требующие уточнения\n/help — помощь\n\n'
        'Работа начинается при запуске скрипта на компьютере. Остановка — закрытием процесса. '
        'Telegram только показывает отчёты; автоматические уведомления — только об успешных откликах.')

STATUS_LABELS = {'found': 'найдена', 'rejected': 'пропущена', 'needs_clarification': 'нужно уточнение',
    'ready': 'готова', 'sending': 'отправляется', 'sent': 'отправлена',
    'unknown': 'результат неизвестен', 'error': 'ошибка', 'closed': 'закрыта'}


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

    def _response(self, text):
        parts = text.strip().split()
        command = parts[0].split('@', 1)[0].lower() if parts else ''
        if command in ('/search', '/pause', '/resume', '/reconcile', '/requeue', '/settings', '/queue'):
            return 'retired', ('Эта команда больше не управляет работой. Telegram только показывает отчёты. '
                'Состояние: /status. Запуск и остановка — на компьютере.')
        if command == '/status':
            counts = self.store.counts()
            blocked = self.store.setting('browser_blocked', False)
            paused = self.store.is_paused()
            lines = ['Скрипт работает; доступ к HH заблокирован.' if blocked else 'Скрипт работает.',
                     'Режим: ' + ('реальная отправка' if self.config.live else 'подготовка без отправки'),
                     'Пауза отправок: ' + ('да' if paused else 'нет'),
                     'Всего: ' + ', '.join(STATUS_LABELS.get(key, key) + ' ' + str(value)
                                           for key, value in sorted(counts.items())),
                     'Уведомления: только успешные отклики. Ответы на команды приходят по запросу.']
            if blocked:
                lines.append('Нужно действие: ' + self.store.setting('attention_reason', 'Проверьте вход и доступ к HH'))
            if paused and counts.get('unknown', 0):
                lines.append('Отправка ждёт проверки предыдущего результата. '
                             + ('Поиск сможет продолжиться после восстановления доступа.' if blocked
                                else 'Поиск вакансий продолжается.'))
            report = self.store.setting('search_report')
            if isinstance(report, dict):
                lines.append('Последний поиск: Вакансий: ' + str(len(report.get('ids', [])))
                             + '. Страниц: ' + str(report.get('pages', 0)) + '.')
                lines.append('Охват неполный.' if report.get('incomplete') else 'Доступная выдача обработана.')
                lines.extend(str(note) for note in report.get('notes', []))
            else:
                lines.append('Результатов поиска пока нет.')
            return 'status', '\n'.join(lines)
        if command == '/clarifications':
            if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r'[1-9][0-9]{0,8}', parts[1])):
                return 'clarifications', 'Укажите положительный номер страницы: /clarifications 1'
            page = int(parts[1]) if len(parts) == 2 else 1
            total = self.store.counts().get('needs_clarification', 0)
            pages = max(1, (total + 19) // 20)
            if page > pages:
                return 'clarifications', 'Такой страницы нет. Доступно страниц: ' + str(pages) + '. /clarifications 1'
            rows = self.store.rows(['needs_clarification'], limit=20, offset=(page - 1) * 20)
            lines = ['Уточнения — страница ' + str(page) + ' из ' + str(pages), 'Всего: ' + str(total)]
            for row in rows:
                vacancy = row['vacancy']
                lines.append(vacancy.id + ' — ' + vacancy.title[:200] + '\n'
                             + row['reason'][:500] + '\n' + vacancy.url)
            if not rows:
                lines.append('Уточнений пока нет.')
            if page < pages:
                lines.append('Следующая страница: /clarifications ' + str(page + 1))
            return 'clarifications', '\n\n'.join(lines)
        if command == '/history':
            statuses = ['sent', 'rejected', 'closed', 'error', 'unknown']
            rows = self.store.rows(statuses, limit=20, newest=True)
            lines = ['История (до 20 записей)']
            for row in rows:
                vacancy = row['vacancy']
                lines.append(vacancy.id + ' — ' + vacancy.title[:200] + '\n'
                    + STATUS_LABELS.get(row['status'], row['status']) + ': ' + row['reason'][:500]
                    + '\n' + vacancy.url)
            if not rows:
                lines.append('Записей пока нет.')
            counts = self.store.counts()
            lines.append('Всего: ' + ', '.join(STATUS_LABELS.get(key, key) + ' ' + str(value)
                                               for key, value in sorted(counts.items())))
            return command[1:], '\n\n'.join(lines)
        if command == '/letter':
            if len(parts) != 2 or not re.fullmatch(r'[0-9]+', parts[1]):
                return 'letter', 'Укажите числовой ID: /letter 123456'
            row = self.store.get(parts[1])
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
        if time.time() < self.store.setting('telegram_next_poll', 0):
            return 0
        offset = self.store.setting('telegram_offset', 0)
        result = await self._api('getUpdates', {'offset': offset, 'timeout': 30,
            'allowed_updates': ['message', 'callback_query']}, 'telegram_next_poll', timeout=40)
        if result is None:
            return 0
        if not isinstance(result, list):
            self.store.set('telegram_next_poll', time.time() + 5)
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
            fresh = update['update_id'] >= self.store.setting('telegram_offset', 0)
            if fresh:
                raw = message.get('text')
                if callback:
                    self.store.command(update['update_id'], 'retired_callback')
                elif authorized and isinstance(raw, str):
                    action, response = self._response(raw)
                    processed += bool(self.store.command(update['update_id'], action, response))
                else:
                    self.store.command(update['update_id'], 'ignored')
            callback_id = callback.get('id')
            if (isinstance(callback_id, str) and callback_id and len(callback_id) <= 512
                    and time.time() >= self.store.setting('telegram_next_poll', 0)):
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
                or time.time() < self.store.setting('telegram_next_poll', 0)
                or time.time() < self.store.setting('telegram_next_cleanup', 0)):
            return
        try:
            await self._api('editMessageReplyMarkup', {'chat_id': self.config.owner_id,
                'message_id': message_id, 'reply_markup': {'inline_keyboard': []}},
                'telegram_next_cleanup', timeout=10)
        except TelegramError:
            # Old/deleted messages may be uneditable. Their actions stay inert.
            pass

    async def flush(self):
        if time.time() < self.store.setting('telegram_next_delivery', 0):
            return 0
        for message in self.store.pending_messages():
            chunks = _chunks(message['text'])
            key = 'telegram_chunk_' + str(message['id'])
            index = self.store.setting(key, 0)
            if type(index) is not int or index < 0 or index > len(chunks):
                raise TelegramError('Нарушен сохранённый прогресс уведомления')
            if index == len(chunks):
                self.store.delivered(message['id'])
                continue
            payload = {'chat_id': self.config.owner_id, 'text': chunks[index],
                       'link_preview_options': {'is_disabled': True}}
            result = await self._api('sendMessage', payload, 'telegram_next_delivery', timeout=20)
            if result is None:
                return 0
            if type(_object(result).get('message_id')) is not int:
                self.store.set('telegram_next_delivery', time.time() + 30)
                raise TelegramError('Telegram не подтвердил доставку уведомления')
            # Persist the acknowledgement before moving to the next chunk.
            self.store.set(key, index + 1)
            self.store.set('telegram_next_delivery', time.time() + 1.05)
            if index + 1 == len(chunks):
                self.store.delivered(message['id'])
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
            self.store.set(retry_key, time.time() + 30)
            raise TelegramError('Не удалось связаться с Telegram; повтор будет позже') from None
        try:
            body = _object(response.json())
        except (json.JSONDecodeError, ValueError):
            body = {}
        if response.status_code == 429 or body.get('error_code') == 429:
            retry_after = _object(body.get('parameters')).get('retry_after', 30)
            if type(retry_after) is not int:
                retry_after = 30
            self.store.set(retry_key, time.time() + max(1, min(retry_after, 3600)))
            return None
        if response.status_code != 200 or body.get('ok') is not True or body.get('result') is None:
            self.store.set(retry_key, time.time() + 30)
            raise TelegramError('Telegram отклонил запрос (HTTP ' + str(response.status_code) + ')')
        return body['result']
