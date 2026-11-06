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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .dedupe import DEDUP_COSINE_THRESHOLD, EMBEDDING_K, cosine
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
            # Не отменяем потребителя посреди операции (иначе открытая
            # транзакция aiosqlite останется висеть с блокировкой записи);
            # завершаем его sentinel-сообщением.
            await self._queue.put((None, None, None))
            try:
                await asyncio.wait_for(self._writer, timeout=10)
            except asyncio.TimeoutError:
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
            if operation is None:  # sentinel: корректная остановка
                return
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

    async def refresh(self, source, vacancy, norm_company=None, norm_title=None, content_hash_value=None):
        return await self._call("refresh", source, vacancy, norm_company, norm_title, content_hash_value)

    async def exact_candidates(self, job_id, norm_company, norm_title):
        """Кандидаты точного ключа (не слияние сами по себе, раздел 8)."""
        return await self._call("exact_candidates", job_id, norm_company, norm_title)

    async def link_candidates(self, links):
        """Прямые ссылки hh/Habr в тексте — жёсткое доказательство тождества."""
        return await self._call("link_candidates", links)

    async def join_cluster(self, job_id, candidates):
        """Кластер: создать или присоединить; представитель — hh при наличии."""
        return await self._call("join_cluster", job_id, candidates)

    async def skipped_cluster(self, candidates):
        """Есть ли среди кандидатов строка пропущенного кластера (16.3)."""
        return await self._call("skipped_cluster", candidates)

    async def embedding_candidates(self, job_id, vector, model, dim, content_hash_value):
        """Сохраняет вектор строки и возвращает top-K «возможно, дубль»."""
        return await self._call("embedding_candidates", job_id, vector, model, dim, content_hash_value)

    async def mark_possible_duplicates(self, job_id, ids):
        return await self._call("mark_possible_duplicates", job_id, ids)

    async def save_assessment(self, job_id, llm):
        """Артефакты writer v2 (оценка/советы/статусы/кэш-штамп) в data.llm."""
        return await self._call("save_assessment", job_id, llm)

    async def log_llm_call(self, job_id, kind, model, prompt_tokens, completion_tokens, cost):
        """Журнал стоимости llm_calls (раздел 9)."""
        return await self._call("log_llm_call", job_id, kind, model, prompt_tokens,
                                completion_tokens, cost)

    async def cluster_skip(self, job_id):
        """Пропуск всего кластера (для /skip, 16.3): статус clusters + все строки."""
        return await self._call("cluster_skip", job_id)

    async def decide(self, job_id, status, reason, grade=None, grade_evidence=None, rules_version=None):
        return await self._call("decide", job_id, status, reason, grade, grade_evidence, rules_version)

    async def reassess(self, job_id, status, reason, letter=None, grade=None,
                       grade_evidence=None, rules_version=None):
        """Переоценка не-терминальной строки (смена правил/профиля, 16.2)."""
        return await self._call("reassess", job_id, status, reason, letter, grade,
                                grade_evidence, rules_version)

    async def stale_row(self, sources=None):
        """Строка, оценённая старыми правилами; переоценивается в первом раунде.

        sources — включённые источники: строки отключённых (например, hh до
        входа) не выбираются и ждут включения источника."""
        return await self._call("stale_row", sources)

    async def make_ready(self, job_id, letter, reason, rules_version=None):
        return await self._call("make_ready", job_id, letter, reason, rules_version)

    async def save_letter(self, job_id, letter):
        return await self._call("save_letter", job_id, letter)

    async def rows(self, statuses=None, limit=50, newest=False, offset=0):
        return await self._call("rows", statuses, limit, newest, offset)

    async def counts(self):
        return await self._call("counts")

    async def first_found(self, sources=None):
        return await self._call("first_found", sources)

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

    async def enqueue(self, text, kind="event", job_id=None, cluster_id=None):
        return await self._call("enqueue", text, kind, job_id, cluster_id)

    async def pending_messages(self):
        return await self._call("pending_messages")

    async def delivered(self, message_id):
        return await self._call("delivered", message_id)

    async def notify_ready(self, job_id, text, cluster_id):
        """Карточка-уведомление: уникально по кластеру/строке (16.3, раздел 5)."""
        return await self._call("notify_ready", job_id, text, cluster_id)

    async def card_exists(self, job_id):
        return await self._call("card_exists", job_id)

    async def next_ready_without_card(self):
        return await self._call("next_ready_without_card")

    async def cluster_rows(self, job_id):
        """Все строки кластера (ссылки для карточки)."""
        return await self._call("cluster_rows", job_id)

    async def keep_job(self, job_id):
        """Ручное решение /keep: needs_clarification → ready (16.2)."""
        return await self._call("keep_job", job_id)

    async def skip_job(self, job_id):
        """Ручное решение /skip: строка и её кластер → skipped (16.3)."""
        return await self._call("skip_job", job_id)

    async def unskip_cluster(self, job_id):
        """Явная отмена пропуска кластера: строки → переоценка."""
        return await self._call("unskip_cluster", job_id)

    async def mark_read(self, job_id):
        """Кнопка карточки нажата: delivered → read (16.2)."""
        return await self._call("mark_read", job_id)

    async def set_letter(self, job_id, letter):
        return await self._call("set_letter", job_id, letter)

    async def delivered_job(self, job_id):
        """Карточка отправлена: ready → delivered (16.2)."""
        return await self._call("delivered_job", job_id)

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
        data = json.loads(row.pop("data"))
        row["possible_duplicates"] = data.pop("possible_duplicates", None)
        row["llm"] = data.pop("llm", None)
        row["vacancy"] = Vacancy.from_dict(data)
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

    async def _op_refresh(self, source, vacancy, norm_company, norm_title, content_hash_value):
        key = source + ":" + str(vacancy.id)
        cursor = await self._conn.execute("SELECT content_hash FROM jobs WHERE id=?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        previous_hash = row[0] if row else None
        await self._conn.execute(
            "UPDATE jobs SET data=?,norm_company=?,norm_title=?,updated=? WHERE id=?",
            (dump(vacancy.to_dict()), norm_company, norm_title, now(), key))
        if content_hash_value is not None:
            if previous_hash is not None and previous_hash != content_hash_value:
                # Данные изменились → переоценка не-терминальной строки (16.2).
                await self._conn.execute(
                    "UPDATE jobs SET content_hash=?,rules_version=NULL WHERE id=?"
                    " AND status NOT IN ('skipped','closed','found','error')",
                    (content_hash_value, key))
            else:
                await self._conn.execute("UPDATE jobs SET content_hash=? WHERE id=?",
                                         (content_hash_value, key))
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

    @staticmethod
    def _source_clause(sources):
        """SQL-условие «id принадлежит одному из источников» (префикс id)."""
        if not sources:
            return "", []
        clauses, args = [], []
        for source in sources:
            clauses.append("(id = ? OR id GLOB ?)")
            args.extend([source, source + ":*"])
        return " AND (" + " OR ".join(clauses) + ")", args

    async def _op_stale_row(self, sources):
        cursor = await self._conn.execute(
            "SELECT value FROM settings WHERE key='rules_version'")
        row = await cursor.fetchone()
        await cursor.close()
        current = json.loads(row[0]) if row else None
        clause, args = self._source_clause(sources)
        cursor = await self._conn.execute(
            "SELECT id FROM jobs WHERE status IN ('ready','rejected','needs_clarification')"
            " AND (rules_version IS NULL OR rules_version != ?)" + clause +
            " ORDER BY created,id LIMIT 1", (current, *args))
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else None

    async def _op_make_ready(self, job_id, letter, reason, rules_version):
        await self._conn.execute(
            "UPDATE jobs SET letter=?,status='ready',reason=?,rules_version=?,updated=?"
            " WHERE id=? AND status='found'",
            (dump(letter.to_dict()) if letter is not None else None, reason, rules_version,
             now(), Store.key(job_id)))
        await self._conn.commit()

    async def _op_save_assessment(self, job_id, llm):
        key = Store.key(job_id)
        cursor = await self._conn.execute("SELECT data FROM jobs WHERE id=?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return
        data = json.loads(row[0])
        data["llm"] = llm
        await self._conn.execute("UPDATE jobs SET data=? WHERE id=?",
                                 (json.dumps(data, ensure_ascii=False), key))
        await self._conn.commit()

    async def _op_log_llm_call(self, job_id, kind, model, prompt_tokens,
                               completion_tokens, cost):
        await self._conn.execute(
            "INSERT INTO llm_calls(job_id,kind,model,prompt_tokens,completion_tokens,cost,created)"
            " VALUES(?,?,?,?,?,?,?)",
            (Store.key(job_id), kind, model, prompt_tokens, completion_tokens, cost, now()))
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

    async def _op_first_found(self, sources):
        clause, args = self._source_clause(sources)
        cursor = await self._conn.execute(
            "SELECT id FROM jobs WHERE status='found'" + clause +
            " ORDER BY created,id LIMIT 1", args)
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

    async def _op_enqueue(self, text, kind, job_id, cluster_id):
        if kind not in ("event", "reply", "success", "notice", "card"):
            raise ValueError("Неизвестный тип уведомления")
        await self._conn.execute(
            "INSERT INTO outbox(text,created,kind,job_id,cluster_id) VALUES(?,?,?,?,?)",
            (text, now(), kind, job_id, cluster_id))
        await self._conn.commit()

    async def _op_pending_messages(self):
        cursor = await self._conn.execute(
            "SELECT * FROM outbox WHERE sent=0 AND kind IN ('reply','success','notice','card')"
            " ORDER BY id LIMIT 20")
        rows = await cursor.fetchall()
        await cursor.close()
        return [_as_dict(row) for row in rows]

    async def _op_delivered(self, message_id):
        await self._conn.execute("UPDATE outbox SET sent=1 WHERE id=?", (message_id,))
        await self._conn.commit()

    async def _op_notify_ready(self, job_id, text, cluster_id):
        key = Store.key(job_id)
        # Уникальность карточки по кластеру/строке (частичные уникальные индексы).
        await self._conn.execute(
            "INSERT OR IGNORE INTO outbox(text,created,kind,job_id,cluster_id)"
            " VALUES(?,?,'card',?,?)", (text, now(), key, cluster_id))
        cursor = await self._conn.execute(
            "SELECT changes()")
        inserted = (await cursor.fetchone())[0]
        await cursor.close()
        await self._conn.commit()
        return inserted > 0

    async def _op_card_exists(self, job_id):
        key = Store.key(job_id)
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE kind='card' AND (job_id=? OR"
            " cluster_id=(SELECT cluster_id FROM jobs WHERE id=?))", (key, key))
        count = (await cursor.fetchone())[0]
        await cursor.close()
        return count > 0

    async def _op_next_ready_without_card(self):
        cursor = await self._conn.execute(
            "SELECT j.id FROM jobs j WHERE j.status='ready' AND NOT EXISTS"
            " (SELECT 1 FROM outbox o WHERE o.kind='card' AND"
            "  (o.job_id=j.id OR (j.cluster_id IS NOT NULL AND o.cluster_id=j.cluster_id)))"
            " ORDER BY j.updated,j.id LIMIT 1")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else None

    async def _op_cluster_rows(self, job_id):
        key = Store.key(job_id)
        cursor = await self._conn.execute("SELECT cluster_id FROM jobs WHERE id=?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or row[0] is None:
            return [await self._op_get(key)]
        cursor = await self._conn.execute(
            "SELECT id FROM jobs WHERE cluster_id=? ORDER BY id", (row[0],))
        ids = [item[0] for item in await cursor.fetchall()]
        await cursor.close()
        return [await self._op_get(item) for item in ids]

    async def _op_keep_job(self, job_id):
        key = Store.key(job_id)
        cursor = await self._conn.execute(
            "UPDATE jobs SET status='ready',manual_decision='keep',manual_decision_at=?,updated=?"
            " WHERE id=? AND status='needs_clarification'", (now(), now(), key))
        changed = cursor.rowcount > 0
        await cursor.close()
        await self._conn.commit()
        return changed

    async def _op_skip_job(self, job_id):
        key = Store.key(job_id)
        cursor = await self._conn.execute(
            "UPDATE jobs SET manual_decision='skip',manual_decision_at=?,updated=? WHERE id=?",
            (now(), now(), key))
        await cursor.close()
        cursor = await self._conn.execute("SELECT cluster_id FROM jobs WHERE id=?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        cluster_id = row[0] if row else None
        if cluster_id is not None:
            await self._conn.execute(
                "UPDATE clusters SET status='skipped',skipped_at=? WHERE id=?", (now(), cluster_id))
            await self._conn.execute(
                "UPDATE jobs SET status='skipped',reason='Пропущен кластер (вручную)',updated=?"
                " WHERE cluster_id=? AND status NOT IN ('skipped','closed')", (now(), cluster_id))
        else:
            await self._conn.execute(
                "UPDATE jobs SET status='skipped',reason='Пропущено вручную (/skip)',updated=?"
                " WHERE id=?", (now(), key))
        await self._conn.commit()
        return True

    async def _op_unskip_cluster(self, job_id):
        key = Store.key(job_id)
        cursor = await self._conn.execute("SELECT cluster_id FROM jobs WHERE id=?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return False
        if row[0] is None:
            # Строка без кластера: отмена ручного пропуска самой строки.
            await self._conn.execute(
                "UPDATE jobs SET status='found',reason='',rules_version=NULL,"
                " manual_decision=NULL,manual_decision_at=NULL,updated=? WHERE id=? AND status='skipped'",
                (now(), key))
            await self._conn.commit()
            return True
        cluster_id = row[0]
        await self._conn.execute(
            "UPDATE clusters SET status='none',skipped_at=NULL WHERE id=?", (cluster_id,))
        # Строки кластера → переоценка в первом раунде (16.2).
        await self._conn.execute(
            "UPDATE jobs SET status='found',reason='',rules_version=NULL,manual_decision=NULL,"
            " manual_decision_at=NULL,updated=? WHERE cluster_id=? AND status='skipped'",
            (now(), cluster_id))
        await self._conn.commit()
        return True

    async def _op_mark_read(self, job_id):
        key = Store.key(job_id)
        await self._conn.execute(
            "UPDATE jobs SET status='read',last_read=?,updated=? WHERE id=? AND status='delivered'",
            (now(), now(), key))
        await self._conn.commit()

    async def _op_set_letter(self, job_id, letter):
        key = Store.key(job_id)
        await self._conn.execute(
            "UPDATE jobs SET letter=?,updated=? WHERE id=? AND status='ready'",
            (dump(letter.to_dict()), now(), key))
        await self._conn.commit()

    async def _op_delivered_job(self, job_id):
        key = Store.key(job_id)
        await self._conn.execute(
            "UPDATE jobs SET status='delivered',updated=? WHERE id=? AND status='ready'",
            (now(), key))
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

    # ---------- дедупликация (раздел 8) ----------

    async def _op_exact_candidates(self, job_id, norm_company, norm_title):
        key = Store.key(job_id)
        if not norm_company or not norm_title:
            return []
        cursor = await self._conn.execute(
            "SELECT id FROM jobs WHERE norm_company=? AND norm_title=? AND id != ? ORDER BY id",
            (norm_company, norm_title, key))
        ids = [row[0] for row in await cursor.fetchall()]
        await cursor.close()
        return ids

    async def _op_link_candidates(self, links):
        ids = []
        for source, native_id in links:
            cursor = await self._conn.execute("SELECT id FROM jobs WHERE id=?",
                                              (source + ":" + str(native_id),))
            row = await cursor.fetchone()
            await cursor.close()
            if row:
                ids.append(row[0])
        return ids

    async def _op_join_cluster(self, job_id, candidates):
        key = Store.key(job_id)
        candidates = [candidate for candidate in candidates if candidate != key]
        cluster_id = None
        for candidate in candidates:
            cursor = await self._conn.execute("SELECT cluster_id FROM jobs WHERE id=?", (candidate,))
            row = await cursor.fetchone()
            await cursor.close()
            if row and row[0] is not None:
                cluster_id = row[0]
                break
        if cluster_id is None:
            rep = sorted([key] + candidates,
                         key=lambda candidate: (0 if candidate.startswith("hh:") else 1, candidate))[0]
            cursor = await self._conn.execute(
                "INSERT INTO clusters(rep_job_id,created,status) VALUES(?,?,'none')", (rep, now()))
            cluster_id = cursor.lastrowid
            await cursor.close()
        await self._conn.execute("UPDATE jobs SET cluster_id=? WHERE id=?", (cluster_id, key))
        for candidate in candidates:
            await self._conn.execute(
                "UPDATE jobs SET cluster_id=? WHERE id=? AND cluster_id IS NULL",
                (cluster_id, candidate))
        if key.startswith("hh:"):
            # Представитель — hh при наличии (16.3).
            await self._conn.execute(
                "UPDATE clusters SET rep_job_id=? WHERE id=? AND rep_job_id NOT LIKE 'hh:%'",
                (key, cluster_id))
        await self._conn.commit()
        return {"kind": "merged", "cluster_id": cluster_id}

    async def _op_skipped_cluster(self, candidates):
        for candidate in candidates:
            cursor = await self._conn.execute(
                "SELECT j.cluster_id,c.status FROM jobs j LEFT JOIN clusters c ON c.id=j.cluster_id"
                " WHERE j.id=?", (candidate,))
            row = await cursor.fetchone()
            await cursor.close()
            if row and row[0] is not None and row[1] == "skipped":
                return True
        return False

    async def _op_embedding_candidates(self, job_id, vector, model, dim, content_hash_value):
        key = Store.key(job_id)
        blob = json.dumps(vector).encode("utf-8")
        await self._conn.execute(
            "INSERT INTO job_embeddings(job_id,embedding,model,dim,content_hash) VALUES(?,?,?,?,?)"
            " ON CONFLICT(job_id) DO UPDATE SET embedding=excluded.embedding,"
            " model=excluded.model,dim=excluded.dim,content_hash=excluded.content_hash",
            (key, blob, model, dim, content_hash_value))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        cursor = await self._conn.execute(
            "SELECT e.job_id,e.embedding FROM job_embeddings e JOIN jobs j ON j.id=e.job_id"
            " WHERE e.job_id != ? AND e.dim=? AND j.status NOT IN ('skipped','closed')"
            " AND j.created >= ?", (key, dim, cutoff))
        rows = await cursor.fetchall()
        await cursor.close()
        scored = []
        for candidate_id, embedding_blob in rows:
            try:
                other = json.loads(bytes(embedding_blob).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            score = cosine(vector, other)
            if score >= DEDUP_COSINE_THRESHOLD:
                scored.append((score, candidate_id))
        scored.sort(reverse=True)
        await self._conn.commit()
        return [candidate_id for _, candidate_id in scored[:EMBEDDING_K]]

    async def _op_mark_possible_duplicates(self, job_id, ids):
        key = Store.key(job_id)
        cursor = await self._conn.execute("SELECT data FROM jobs WHERE id=?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return
        data = json.loads(row[0])
        data["possible_duplicates"] = list(ids)
        await self._conn.execute("UPDATE jobs SET data=? WHERE id=?",
                                 (json.dumps(data, ensure_ascii=False), key))
        await self._conn.commit()

    async def _op_cluster_skip(self, job_id):
        key = Store.key(job_id)
        cursor = await self._conn.execute("SELECT cluster_id FROM jobs WHERE id=?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or row[0] is None:
            return False
        cluster_id = row[0]
        await self._conn.execute("UPDATE clusters SET status='skipped',skipped_at=? WHERE id=?", (now(), cluster_id))
        await self._conn.execute(
            "UPDATE jobs SET status='skipped',reason='Пропущен кластер (вручную)',updated=?"
            " WHERE cluster_id=? AND status NOT IN ('skipped','closed')", (now(), cluster_id))
        await self._conn.commit()
        return True

    async def _op_round_commit(self, source, t_start, report):
        """Атомарный коммит источника (R5): строки + отчёт + last_search."""
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            for hit in report.hits:
                native_id = str(hit.native_id)
                key = source + ":" + native_id
                placeholder = {"id": native_id, "title": "Вакансия ещё не прочитана", "description": ""}
                await self._conn.execute(
                    "INSERT OR IGNORE INTO jobs(id,data,status,created,updated)"
                    " VALUES(?,?,'found',?,?)",
                    (key, dump(placeholder), now(), now()))
                # У известного поста — только last_seen (16.1).
                await self._conn.execute("UPDATE jobs SET last_seen=? WHERE id=?", (t_start, key))
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
