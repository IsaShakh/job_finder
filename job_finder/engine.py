"""Движок раундов поиска (этап 2 плана, контракт раунда 16.5).

Раунд запускается командой /search в Telegram или CLI `search`; источники
опрашиваются параллельно, коммит каждого источника атомарен. Сбой одной
платформы изолирован: таймер `next_retry:<source>` и состояние `waiting`;
остальные источники продолжают работу.
"""
import asyncio
import time
from datetime import datetime, timezone
from .models import AttentionRequired, RateLimited, VacancyUnavailable
from .rules import evaluate
from .store import Store

# Правило тестов после удаления автоотклика (решение 05.11.2026):
# тест не отклоняет вакансию, а помечается в карточке.
TEST_LABEL = "Тест: потребуется пройти при отклике"
RETRY_DELAY = 300  # секунд до повтора источника после сбоя/лимита


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Engine:
    def __init__(self, config, store, adapters, writer):
        self.config=config
        self.store=store
        self.adapters=adapters
        self.writer=writer

    async def start(self):
        # Прерванный active-раунд доигрывается идемпотентно (R5);
        # позиция пагинации не персистится, коммиты источников атомарны.
        await self.store.recover_rounds()
        await self.store.set('browser_blocked',False)
        await self.store.set('attention_reason','')

    async def attention(self, message):
        await self.store.pause(True)
        # Stop work until the user fixes access and restarts locally.
        await self.store.set("browser_blocked",True)
        await self.store.set("attention_reason",message)
        await self.store.enqueue("Нужно действие: "+message+"\nРабота приостановлена.")
        print("Работа остановлена: "+message+". После исправления перезапустите скрипт.")

    def _adapter_for(self, job_id):
        source = str(job_id).split(":", 1)[0]
        for adapter in self.adapters:
            if adapter.name == source:
                return adapter
        return None

    async def _search_one(self, adapter, t_start):
        since = await self.store.setting('last_search:' + adapter.name)
        return await adapter.search(None, since)

    async def _run_round(self, round_id, pending=None):
        t_start = utc_now()
        pending = {adapter.name for adapter in self.adapters} if pending is None else set(pending)
        while pending:
            active = [adapter for adapter in self.adapters if adapter.name in pending]
            results = await asyncio.gather(
                *(self._search_one(adapter, t_start) for adapter in active),
                return_exceptions=True)
            for adapter, result in zip(active, results):
                if isinstance(result, RateLimited):
                    await self.store.set('next_retry:' + adapter.name, time.time() + RETRY_DELAY)
                elif isinstance(result, AttentionRequired):
                    await self.attention(str(result))
                    await self.store.set('next_retry:' + adapter.name, time.time() + RETRY_DELAY)
                elif isinstance(result, BaseException):
                    await self.store.set('next_retry:' + adapter.name, time.time() + RETRY_DELAY)
                    await self.store.enqueue("Источник %s не опрошен: %s" % (adapter.name, type(result).__name__))
                else:
                    await self.store.round_commit(adapter.name, t_start, result)
                    pending.discard(adapter.name)
            if pending:
                await self.store.round_waiting(round_id, pending)
                return
        await self.store.round_done(round_id)
        await self.store.enqueue(await self._round_summary())

    async def _round_summary(self):
        lines = ["Раунд поиска завершён."]
        for adapter in self.adapters:
            report = await self.store.setting('search_report:' + adapter.name)
            if isinstance(report, dict):
                coverage = "охват неполный" if report.get("incomplete") else "выдача обработана"
                notes = "; ".join(report.get("notes") or [])
                lines.append("%s: вакансий %s, %s%s" % (
                    adapter.name, len(report.get("ids") or []), coverage,
                    ("; " + notes) if notes else ""))
            else:
                lines.append("%s: нет отчёта" % adapter.name)
        return "\n".join(lines)

    async def run_pending_rounds(self):
        while True:
            round_id = await self.store.round_next()
            if round_id is None:
                return
            await self._run_round(round_id)

    async def resume_waiting(self):
        """Возобновляет waiting-раунд, когда все next_retry-таймеры прошли."""
        resumed = await self.store.round_resumable()
        if resumed is None:
            return False
        round_id, pending = resumed
        await self._run_round(round_id, pending=pending)
        return True

    async def prepare(self, job_id):
        row = await self.store.get(job_id)
        if not row or row["status"]!="found":
            return
        source = str(job_id).split(":", 1)[0]
        adapter = self._adapter_for(job_id)
        if adapter is None:
            await self.store.decide(job_id,"error","Неизвестный источник: "+source)
            return
        try:
            vacancy=await adapter.fetch(Store.native(job_id))
            await self.store.refresh(source, vacancy)
            decision=evaluate(vacancy)
            reason=decision.reason
            if vacancy.test_required and decision.status in ("ready","needs_clarification"):
                reason += " · " + TEST_LABEL
            if decision.status=="ready":
                letter=await self.writer.generate(vacancy)
                await self.store.make_ready(job_id,letter,reason)
            else:
                await self.store.decide(job_id,decision.status,reason)
                if decision.status=="needs_clarification":
                    await self.store.enqueue("Нужно уточнение: "+vacancy.title+"\n"+vacancy.url+"\n"+reason)
        except VacancyUnavailable as exc:
            await self.store.decide(job_id,"needs_clarification",str(exc))
        except AttentionRequired as exc:
            await self.attention(str(exc))
        except Exception as exc:
            await self.store.decide(job_id,"error","Подготовка не удалась: "+type(exc).__name__)
            await self.store.enqueue("Не удалось подготовить вакансию "+Store.native(job_id)+" ("+type(exc).__name__+"). Повтор возможен вручную.")

    async def digest(self, skipped=None):
        labels={"found":"ждут чтения","read":"прочитаны","ready":"готовы","delivered":"доставлены","rejected":"отсеяны","needs_clarification":"уточнение","skipped":"пропущены","error":"ошибки","closed":"закрыты"}
        counts = await self.store.counts()
        text="Сводка очереди\n"+"\n".join(labels.get(k,k)+": "+str(v) for k,v in counts.items())
        cutoff=await self.store.setting('last_digest_at','1970-01-01T00:00:00+00:00')
        rows=[row for row in await self.store.rows(limit=100000) if row['id']!='hh:137924141']
        text+='\n\nС предыдущей сводки: новых %s, готово %s, отсеяно %s.' % (sum(row['created']>=cutoff for row in rows),sum(row['status']=='ready' and row['updated']>=cutoff for row in rows),sum(row['status']=='rejected' and row['updated']>=cutoff for row in rows))
        skipped=await self.store.pending_skips() if skipped is None else skipped
        if skipped:
            text+="\n\nПропуски из-за теста с предыдущей сводки:\n"+"\n".join('https://hh.ru/vacancy/'+r['job_id']+" — "+r["reason"] for r in skipped)
        return text

    async def tick(self):
        if await self.store.setting("browser_blocked",False):
            return
        await self.run_pending_rounds()
        if await self.resume_waiting():
            return
        job_id=await self.store.first_found()
        if job_id:
            await self.prepare(job_id)
            return
        day=datetime.now().astimezone().date().isoformat()
        if await self.store.setting("digest_day") != day:
            skipped=await self.store.pending_skips()
            await self.store.queue_digest(await self.digest(skipped),day,[row['id'] for row in skipped])

    async def run(self):
        await self.start()
        while True:
            await self.tick()
            await asyncio.sleep(0.5)
