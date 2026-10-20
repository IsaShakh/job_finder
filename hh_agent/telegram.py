"""Owner-only Telegram control with durable updates and notification chunks.

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

HELP = ('Команды:\n/search — начать поиск\n/pause — пауза отправок\n'
        '/resume — продолжить очередь в текущем режиме\n/queue — очередь\n'
        '/history — история\n/settings — настройки\n/letter ID — письмо и подготовка\n'
        '/reconcile — проверить незавершённые отклики\n/requeue ID — повторно оценить пропущенную вакансию\n/help — помощь\n\n'
        '/resume не включает реальную отправку. Режим меняется при запуске приложения.')

BUTTONS = {'inline_keyboard': [
    [{'text': 'Поиск', 'callback_data': '/search'}, {'text': 'Очередь', 'callback_data': '/queue'}],
    [{'text': 'Пауза', 'callback_data': '/pause'}, {'text': 'Продолжить', 'callback_data': '/resume'}],
    [{'text': 'История', 'callback_data': '/history'}, {'text': 'Настройки', 'callback_data': '/settings'}],
    [{'text': 'Проверить отклики', 'callback_data': '/reconcile'}],
]}
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
        if command == '/pause':
            return 'pause', 'Пауза включена. Уже начатая операция завершится и попадёт в историю.'
        if command == '/resume':
            mode = 'реальной отправки' if self.config.live else 'подготовки без отправки'
            return 'resume', 'Пауза снята. Сохранён режим ' + mode + '.'
        if command == '/search':
            return 'search', 'Поиск запрошен. Результаты появятся в очереди и сводке.'
        if command == '/reconcile':
            return 'reconcile', 'Проверка незавершённых откликов запрошена.'
        if command == '/requeue':
            if len(parts) != 2 or not re.fullmatch(r'[0-9]+', parts[1]):
                return 'requeue_denied', 'Укажите числовой ID: /requeue 123456'
            row = self.store.get(parts[1])
            if row is None or row['status'] not in ('rejected', 'needs_clarification', 'error', 'closed'):
                return 'requeue_denied', ('Повторная оценка недоступна: нужна ранее пропущенная вакансия '
                    'без начатого или подтверждённого отклика. Для неизвестного результата используйте /reconcile.')
            return 'requeue:' + parts[1], 'Повторная оценка вакансии ' + parts[1] + ' запрошена.'
        if command == '/settings':
            return 'settings', ('Режим: ' + ('реальная отправка' if self.config.live else 'подготовка без отправки')
                + '\nПауза: ' + ('да' if self.store.is_paused() else 'нет')
                + '\nРезюме: ' + self.config.resume_id
                + '\nПоиск каждые ' + str(self.config.search_interval) + ' сек.'
                + '\nИнтервал отправки: ' + str(self.config.send_interval) + ' сек.'
                + '\nМодель: ' + ('настроена' if self.config.llm_model and self.config.llm_api_key else 'не настроена'))
        if command in ('/queue', '/history'):
            statuses = (['found', 'ready', 'sending', 'unknown', 'needs_clarification', 'error']
                        if command == '/queue' else ['sent', 'rejected', 'closed', 'error', 'unknown'])
            rows = self.store.rows(statuses, limit=20, newest=(command == '/history'))
            heading = 'Очередь (до 20 записей)' if command == '/queue' else 'История (до 20 записей)'
            lines = [heading]
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
            if update['update_id'] >= self.store.setting('telegram_offset', 0):
                raw = callback.get('data') if callback else message.get('text')
                if authorized and isinstance(raw, str):
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
                    # The command and offset are already durable. Do not replay either.
                    pass
        return processed

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
            if index == len(chunks) - 1:
                payload['reply_markup'] = BUTTONS
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
