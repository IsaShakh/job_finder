"""AsyncStore: aiosqlite + один потребитель через asyncio.Queue (R8).

Все операции идут через одну очередь к одному соединению aiosqlite
(aiosqlite выполняет каждый шаг в отдельном потоке — event loop не
блокируется; очередь сериализует записи от параллельных адаптеров).
Схема и миграция (бэкап, user_version 3→4) выполняются синхронным
`Store` при создании объекта.

Раунд поиска (контракт 16.5): `round_requests` сохраняется при приёме
команды /search ДО квитирования обновления Telegram; коммит источника —
строки + `search_report:<source>` + `last_search:<source>=t_start` одной
транзакцией.
"""
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .models import Letter, Vacancy
from .store import Store, dump, now


def _now():
    return datetime.now(timezone.utc).isoformat()


def _as_dict(row):
    if row is None:
        return None
    return dict(zip(row.keys(), row))


class AsyncStore:
    def __init__(self, path):
        # Схема, миграция (с бэкапом), сиды — синхронным Store до открытия.
        self.path = Path(path)
        bootstrap = Store(self.path)
        bootstrap.close()
        self._conn = None
        self._queue = None
        self._writer = None

    async def start(self):
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(str(self.path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("CREATE TABLE IF NOT EXISTS round_requests ("
                                 "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                                 "state TEXT NOT NULL,"
                                 "pending TEXT NOT NULL DEFAULT '[]',"
                                 "created TEXT NOT NULL, updated TEXT NOT NULL)")
        await self._conn.commit()
        self._queue = asyncio.Queue()
        self._writer = asyncio.create_task(self._consume())

    async def close(self):
        if self._writer is not None:
            self._writer.cancel()
            try:
                await self._writer
            except asyncio.CancelledError:
                pass
            self._writer = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _consume(self):
        while True:
            operation, args, future = await self._queue.get()
            try:
                result = await getattr(self, "_op_" + operation)(*args)
            except BaseException as exc:
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(result)

    async def _call(self, operation, *args):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self._queue.put((operation, args, future))
        return await future

    # ---------- таблица jobs ----------

    async def get(self, job_id):
        return await self._call("get", job_id)

    async def discover(self, source, vacancy):
        return await self._call("discover", source, vacancy)

    async def refresh(self, source, vacancy):
        return await self._call("refresh", source, vacancy)

    async def decide(self, job_id, status, reason, grade=None, grade_evidence=None, rules_version=None):
        return await self._call("decide", job_id, status, reason, grade, grade_evidence, rules_version)

    async def reassess(self, job_id, status, reason, letter=None, grade=None,
                       grade_evidence=None, rules_version=None):
        """Переоценка не-терминальной строки (смена правил/профиля, 16.2)."""
        return await self._call("reassess", job_id, status, reason, letter, grade,
                                grade_evidence, rules_version)

    async def stale_row(self):
        """Строка, оценённая старыми правилами; переоценивается в первом раунде."""
        return await self._call("stale_row")

    async def make_ready(self, job_id, letter, reason):
        return await self._call("make_ready", job_id, letter, reason)

    async def save_letter(self, job_id, letter):
        return await self._call("save_letter", job_id, letter)

    async def rows(self, statuses=None, limit=50, newest=False, offset=0):
        return await self._call("rows", statuses, limit, newest, offset)

    async def counts(self):
        return await self._call("counts")

    async def first_found(self):
        return await self._call("first_found")

    # ---------- настройки ----------

    async def setting(self, key, default=None):
        return await self._call("setting", key, default)

    async def set(self, key, value):
        return await self._call("set", key, value)

    async def pause(self, paused=True):
        return await self._call("set", "paused", paused)

    async def is_paused(self):
        return await self._call("setting", "paused", True)

    # ---------- outbox/inbox ----------

    async def enqueue(self, text, kind="event"):
        return await self._call("enqueue", text, kind)

    async def pending_messages(self):
        return await self._call("pending_messages")

    async def delivered(self, message_id):
        return await self._call("delivered", message_id)

    async def command(self, update_id, action, response=None):
        return await self._call("command", update_id, action, response)

    async def pending_skips(self):
        return await self._call("pending_skips")

    async def queue_digest(self, text, day, skip_ids):
        return await self._call("queue_digest", text, day, skip_ids)

    # ---------- раунд поиска (16.5) ----------

    async def round_request(self):
        """Приём команды /search: строка round_requests, если раунда ещё нет."""
        return await self._call("round_request")

    async def round_next(self):
        """Берёт queued → active; возвращает id или None."""
        return await self._call("round_next")

    async def round_resumable(self):
        """Берёт waiting, чьи next_retry-таймеры прошли → active."""
        return await self._call("round_resumable")

    async def round_waiting(self, round_id, pending):
        return await self._call("round_waiting", round_id, pending)

    async def round_done(self, round_id):
        return await self._call("round_done", round_id)

    async def recover_rounds(self):
        """Восстановление: прерванный active-раунд возвращается в queued."""
        return await self._call("recover_rounds")

    async def round_commit(self, source, t_start, report):
        """Атомарный коммит источника: строки + отчёт + last_search (R5)."""
        return await self._call("round_commit", source, t_start, report)

    # ---------- операции (один потребитель) ----------

    async def _op_get(self, job_id):
        cursor = await self._conn.execute("SELECT * FROM jobs WHERE id=?", (Store.key(job_id),))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        row = _as_dict(row)
        row["vacancy"] = Vacancy.from_dict(json.loads(row.pop("data")))
        row["letter"] = Letter(**json.loads(row["letter"])) if row["letter"] else None
        return row

    async def _op_discover(self, source, vacancy):
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO jobs(id,data,status,created,updated) VALUES(?,?,'found',?,?)",
            (source + ":" + str(vacancy.id), dump(vacancy.to_dict()), now(), now()))
        result = cursor.rowcount > 0
        await cursor.close()
        await self._conn.commit()
        return result

    async def _op_refresh(self, source, vacancy):
        await self._conn.execute("UPDATE jobs SET data=?,updated=? WHERE id=?",
                                 (dump(vacancy.to_dict()), now(), source + ":" + str(vacancy.id)))
        await self._conn.commit()

    async def _op_decide(self, job_id, status, reason, grade, grade_evidence, rules_version):
        from .store import STATES, TERMINAL
        if status not in STATES:
            raise ValueError("Неизвестный статус")
        placeholders = ",".join("?" for _ in TERMINAL)
        await self._conn.execute(
            "UPDATE jobs SET status=?,reason=?,grade=?,grade_evidence=?,rules_version=?,updated=?"
            " WHERE id=? AND status NOT IN (" + placeholders + ")",
            (status, reason, grade, grade_evidence, rules_version, now(), Store.key(job_id), *TERMINAL))
        await self._conn.commit()

    async def _op_reassess(self, job_id, status, reason, letter, grade, grade_evidence, rules_version):
        from .store import STATES
        if status not in STATES:
            raise ValueError("Неизвестный статус")
        await self._conn.execute(
            "UPDATE jobs SET status=?,reason=?,letter=?,grade=?,grade_evidence=?,rules_version=?,updated=?"
            " WHERE id=? AND status IN ('ready','rejected','needs_clarification')",
            (status, reason, dump(letter.to_dict()) if letter is not None else None,
             grade, grade_evidence, rules_version, now(), Store.key(job_id)))
        await self._conn.commit()

    async def _op_stale_row(self):
        cursor = await self._conn.execute(
            "SELECT value FROM settings WHERE key='rules_version'")
        row = await cursor.fetchone()
        await cursor.close()
        current = json.loads(row[0]) if row else None
        cursor = await self._conn.execute(
            "SELECT id FROM jobs WHERE status IN ('ready','rejected','needs_clarification')"
            " AND (rules_version IS NULL OR rules_version != ?)"
            " ORDER BY created,id LIMIT 1", (current,))
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else None

    async def _op_make_ready(self, job_id, letter, reason):
        await self._conn.execute(
            "UPDATE jobs SET letter=?,status='ready',reason=?,updated=? WHERE id=? AND status='found'",
            (dump(letter.to_dict()), reason, now(), Store.key(job_id)))
        await self._conn.commit()

    async def _op_save_letter(self, job_id, letter):
        from .store import TERMINAL
        placeholders = ",".join("?" for _ in TERMINAL)
        await self._conn.execute(
            "UPDATE jobs SET letter=?,updated=? WHERE id=? AND status NOT IN (" + placeholders + ")",
            (dump(letter.to_dict()), now(), Store.key(job_id), *TERMINAL))
        await self._conn.commit()

    async def _op_rows(self, statuses, limit, newest, offset):
        sql = "SELECT id FROM jobs"
        args = []
        if statuses:
            sql += " WHERE status IN (" + ",".join("?" for _ in statuses) + ")"
            args.extend(statuses)
        sql += " ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?" if newest else " ORDER BY created,id LIMIT ? OFFSET ?"
        args.extend((limit, offset))
        cursor = await self._conn.execute(sql, args)
        ids = [row[0] for row in await cursor.fetchall()]
        await cursor.close()
        return [await self._op_get(job_id) for job_id in ids]

    async def _op_counts(self):
        cursor = await self._conn.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status")
        result = dict(await cursor.fetchall())
        await cursor.close()
        return result

    async def _op_first_found(self):
        cursor = await self._conn.execute(
            "SELECT id FROM jobs WHERE status='found' ORDER BY created,id LIMIT 1")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else None

    async def _op_setting(self, key, default):
        cursor = await self._conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        return json.loads(row[0]) if row else default

    async def _op_set(self, key, value):
        await self._conn.execute(
            "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, dump(value)))
        await self._conn.commit()

    async def _op_enqueue(self, text, kind):
        if kind not in ("event", "reply", "success"):
            raise ValueError("Неизвестный тип уведомления")
        await self._conn.execute("INSERT INTO outbox(text,created,kind) VALUES(?,?,?)",
                                 (text, now(), kind))
        await self._conn.commit()

    async def _op_pending_messages(self):
        cursor = await self._conn.execute(
            "SELECT * FROM outbox WHERE sent=0 AND kind IN ('reply','success') ORDER BY id LIMIT 20")
        rows = await cursor.fetchall()
        await cursor.close()
        return [_as_dict(row) for row in rows]

    async def _op_delivered(self, message_id):
        await self._conn.execute("UPDATE outbox SET sent=1 WHERE id=?", (message_id,))
        await self._conn.commit()

    async def _op_command(self, update_id, action, response):
        """Приём команды Telegram: inbox + offset + (для /search) round_requests
        — в одной транзакции, round_requests ДО продвижения offset (R5)."""
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self._conn.execute(
                "INSERT OR IGNORE INTO inbox VALUES(?,?,?)", (update_id, action, now()))
            recorded = cursor.rowcount > 0
            await cursor.close()
            if recorded:
                if action == "search":
                    cursor = await self._conn.execute(
                        "SELECT COUNT(*) FROM round_requests WHERE state IN ('queued','active')")
                    busy = (await cursor.fetchone())[0] > 0
                    await cursor.close()
                    if not busy:
                        await self._conn.execute(
                            "INSERT INTO round_requests(state,pending,created,updated)"
                            " VALUES('queued','[]',?,?)", (now(), now()))
                        response = "Поиск запущен. Результаты — после завершения раунда."
                    else:
                        response = "Поиск уже выполняется. Новая команда не создаёт второй раунд."
                await self._conn.execute(
                    "INSERT INTO settings VALUES ('telegram_offset',?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (dump(update_id + 1),))
                if response:
                    await self._conn.execute(
                        "INSERT INTO outbox(text,created,kind) VALUES(?,?,'reply')",
                        (response, now()))
            await self._conn.commit()
            return recorded
        except BaseException:
            await self._conn.rollback()
            raise

    async def _op_pending_skips(self):
        cursor = await self._conn.execute("SELECT * FROM skip_events WHERE reported=0 ORDER BY id")
        rows = await cursor.fetchall()
        await cursor.close()
        return [_as_dict(row) for row in rows]

    async def _op_queue_digest(self, text, day, skip_ids):
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            await self._conn.execute("INSERT INTO outbox(text,created,kind) VALUES(?,?,'event')",
                                     (text, now()))
            for event_id in skip_ids:
                await self._conn.execute("UPDATE skip_events SET reported=1 WHERE id=?",
                                         (event_id,))
            await self._conn.execute(
                "INSERT INTO settings VALUES ('digest_day',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dump(day),))
            await self._conn.execute(
                "INSERT INTO settings VALUES ('last_digest_at',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dump(now()),))
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise

    async def _op_round_request(self):
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM round_requests WHERE state IN ('queued','active')")
        busy = (await cursor.fetchone())[0] > 0
        await cursor.close()
        if not busy:
            await self._conn.execute(
                "INSERT INTO round_requests(state,pending,created,updated) VALUES('queued','[]',?,?)",
                (now(), now()))
            await self._conn.commit()
            return True
        return False

    async def _op_round_next(self):
        cursor = await self._conn.execute(
            "SELECT id FROM round_requests WHERE state='queued' ORDER BY id LIMIT 1")
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        round_id = row[0]
        await self._conn.execute(
            "UPDATE round_requests SET state='active',updated=? WHERE id=?", (now(), round_id))
        await self._conn.commit()
        return round_id

    async def _op_round_resumable(self):
        """Возвращает (id, pending) waiting-раунда, чьи таймеры прошли, или None."""
        cursor = await self._conn.execute(
            "SELECT id,pending FROM round_requests WHERE state='waiting' ORDER BY id LIMIT 1")
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        round_id, pending_raw = row
        cursor = await self._conn.execute(
            "SELECT value FROM settings WHERE key LIKE 'next_retry:%'")
        retries = [value for (value,) in await cursor.fetchall()]
        await cursor.close()
        now_ts = time.time()
        for value in retries:
            try:
                if float(value) > now_ts:
                    return None
            except (TypeError, ValueError):
                pass
        try:
            pending = list(json.loads(pending_raw))
        except ValueError:
            pending = []
        await self._conn.execute(
            "UPDATE round_requests SET state='active',updated=? WHERE id=?", (now(), round_id))
        await self._conn.commit()
        return round_id, pending

    async def _op_round_waiting(self, round_id, pending):
        await self._conn.execute(
            "UPDATE round_requests SET state='waiting',pending=?,updated=? WHERE id=?",
            (dump(list(pending)), now(), round_id))
        await self._conn.commit()

    async def _op_round_done(self, round_id):
        await self._conn.execute(
            "UPDATE round_requests SET state='done',pending='[]',updated=? WHERE id=?",
            (now(), round_id))
        await self._conn.commit()

    async def _op_recover_rounds(self):
        await self._conn.execute(
            "UPDATE round_requests SET state='queued',updated=? WHERE state='active'", (now(),))
        await self._conn.commit()

    async def _op_round_commit(self, source, t_start, report):
        """Атомарный коммит источника (R5): строки + отчёт + last_search."""
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            for hit in report.hits:
                native_id = str(hit.native_id)
                placeholder = {"id": native_id, "title": "Вакансия ещё не прочитана", "description": ""}
                await self._conn.execute(
                    "INSERT OR IGNORE INTO jobs(id,data,status,created,updated)"
                    " VALUES(?,?,'found',?,?)",
                    (source + ":" + native_id, dump(placeholder), now(), now()))
            summary = {"ids": [str(hit.native_id) for hit in report.hits],
                       "incomplete": report.incomplete, "notes": list(report.notes)}
            await self._conn.execute(
                "INSERT INTO settings VALUES ('search_report:'||?1,?2)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (source, dump(summary)))
            await self._conn.execute(
                "INSERT INTO settings VALUES ('last_search:'||?1,?2)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (source, dump(t_start)))
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise
