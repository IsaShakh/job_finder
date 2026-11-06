"""Движок раундов поиска (этап 2 плана, контракт раунда 16.5).

Раунд запускается командой /search в Telegram или CLI `search`; источники
опрашиваются параллельно, коммит каждого источника атомарен. Сбой одной
платформы изолирован: таймер `next_retry:<source>` и состояние `waiting`;
остальные источники продолжают работу.
"""
import asyncio
import time
from datetime import datetime, timezone
from .dedupe import (EmbeddingClient, content_hash, embedding_input,
                     extract_links, normalize_company, normalize_title)
from .models import AttentionRequired, RateLimited, VacancyUnavailable
from .rules import evaluate_all
from .store import Store
from .writer import PROMPT_VERSION, Writer

# Правило тестов после удаления автоотклика (решение 05.11.2026):
# тест не отклоняет вакансию, а помечается в карточке.
TEST_LABEL = "Тест: потребуется пройти при отклике"
RETRY_DELAY = 300  # секунд до повтора источника после сбоя/лимита


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Engine:
    def __init__(self, config, store, adapters, writer, embeddings=None):
        self.config=config
        self.store=store
        self.adapters=adapters
        self.writer=writer
        self.embeddings = embeddings if embeddings is not None else EmbeddingClient(config)

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

    async def _card(self, job_id, vacancy, hash_value, current_rules):
        """Оценка + письмо + советы с кэшем по версии содержимого (R7)."""
        profile_version = await self.store.setting('profile_version')
        stamp = {"content_hash": hash_value, "rules_version": current_rules,
                 "profile_version": profile_version, "model": self.config.llm_model,
                 "prompt": PROMPT_VERSION}
        row = await self.store.get(job_id)
        cached = (row.get("llm") or {}).get("stamp") if row else None
        if cached == stamp and row.get("letter") and (row.get("llm") or {}).get("score_status") in ("ok", "unavailable"):
            return row["letter"]  # содержимое/правила/профиль не менялись — без вызова LLM
        result = await self.writer.generate(vacancy)
        llm = {"score": result.score, "score_reason": result.score_reason,
               "score_status": result.score_status,
               "advice": [{"text": item.text, "grounds": item.grounds} for item in result.advice],
               "advice_status": result.advice_status,
               "letter_status": result.letter_status,
               "stamp": stamp}
        await self.store.save_assessment(job_id, llm)
        if result.usage and result.usage.get("prompt_tokens") is not None:
            await self.store.log_llm_call(job_id, "card", result.usage.get("model"),
                                          result.usage.get("prompt_tokens"),
                                          result.usage.get("completion_tokens"),
                                          Writer._cost(result.usage))
        return result.letter

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
            norm_company = normalize_company(vacancy.company)
            norm_title = normalize_title(vacancy.title)
            hash_value = content_hash(vacancy)
            await self.store.refresh(source, vacancy,
                                     norm_company=norm_company,
                                     norm_title=norm_title,
                                     content_hash_value=hash_value)
            # Дедупликация (раздел 8): точный ключ и прямые ссылки — кандидаты,
            # сливает прямая ссылка или точный ключ, подтверждённый эмбеддингом.
            exact = await self.store.exact_candidates(job_id, norm_company, norm_title)
            links = await self.store.link_candidates(extract_links(vacancy))
            candidates = set(exact) | set(links)
            if candidates and await self.store.skipped_cluster(candidates):
                await self.store.decide(job_id, "skipped", "Пропущен кластер (ранее /skip)")
                return
            topk = []
            if not links and self.embeddings.available:
                try:
                    vector, dim, model = await self.embeddings.embed(embedding_input(vacancy))
                    topk = await self.store.embedding_candidates(job_id, vector, model, dim, hash_value)
                except RuntimeError:
                    pass  # сбой эмбеддингов не блокирует конвейер
            if topk and await self.store.skipped_cluster(topk):
                await self.store.decide(job_id, "skipped", "Пропущен кластер (ранее /skip)")
                return
            if links:
                await self.store.join_cluster(job_id, sorted(links))
            elif exact and [candidate for candidate in exact if candidate in topk]:
                await self.store.join_cluster(job_id, [candidate for candidate in exact if candidate in topk])
            else:
                possible = sorted(candidates | set(topk))
                if possible:
                    await self.store.mark_possible_duplicates(job_id, possible)
            assessment=evaluate_all(vacancy, self.config.profiles)
            reason=assessment.reason
            if vacancy.test_required and assessment.status in ("ready","needs_clarification"):
                reason += " · " + TEST_LABEL
            current_rules = await self.store.setting('rules_version')
            if assessment.status=="ready":
                letter=await self._card(job_id, vacancy, hash_value, current_rules)
                await self.store.make_ready(job_id,letter,reason,current_rules)
            else:
                await self.store.decide(job_id,assessment.status,reason,
                                        grade=assessment.grade,
                                        grade_evidence=assessment.grade_evidence,
                                        rules_version=current_rules)
                if assessment.status=="needs_clarification":
                    await self.store.enqueue("Нужно уточнение: "+vacancy.title+"\n"+vacancy.url+"\n"+reason+"\n/keep "+Store.native(job_id)+" — подходит\n/skip "+Store.native(job_id)+" — пропустить", kind="notice")
        except VacancyUnavailable as exc:
            await self.store.decide(job_id,"needs_clarification",str(exc))
        except AttentionRequired as exc:
            await self.attention(str(exc))
        except Exception as exc:
            await self.store.decide(job_id,"error","Подготовка не удалась: "+type(exc).__name__)
            await self.store.enqueue("Не удалось подготовить вакансию "+Store.native(job_id)+" ("+type(exc).__name__+"). Повтор возможен вручную.")

    async def reassess(self, job_id):
        """Переоценка строки, решённой старыми правилами (16.2, раздел 12)."""
        row = await self.store.get(job_id)
        if row is None:
            return
        adapter = self._adapter_for(job_id)
        if adapter is None:
            return
        current_rules = await self.store.setting('rules_version')
        try:
            vacancy=await adapter.fetch(Store.native(job_id))
            source = str(job_id).split(":", 1)[0]
            await self.store.refresh(source, vacancy,
                                     norm_company=normalize_company(vacancy.company),
                                     norm_title=normalize_title(vacancy.title),
                                     content_hash_value=content_hash(vacancy))
            assessment=evaluate_all(vacancy, self.config.profiles)
            reason=assessment.reason
            if vacancy.test_required and assessment.status in ("ready","needs_clarification"):
                reason += " · " + TEST_LABEL
            if assessment.status=="ready":
                letter=await self._card(job_id, vacancy, content_hash(vacancy), current_rules)
                await self.store.reassess(job_id,"ready",reason,letter=letter,
                                          grade=assessment.grade,
                                          grade_evidence=assessment.grade_evidence,
                                          rules_version=current_rules)
            else:
                await self.store.reassess(job_id,assessment.status,reason,
                                          grade=assessment.grade,
                                          grade_evidence=assessment.grade_evidence,
                                          rules_version=current_rules)
        except VacancyUnavailable as exc:
            await self.store.reassess(job_id,"needs_clarification",str(exc),rules_version=current_rules)
        except AttentionRequired as exc:
            await self.attention(str(exc))
        except Exception as exc:
            await self.store.decide(job_id,"error","Переоценка не удалась: "+type(exc).__name__)

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

    async def notify_ready(self, job_id):
        """Уведомление «Нашлась новая релевантная вакансия» (раздел 10)."""
        if not await self.store.setting('DELIVER', True):
            return False  # DELIVER=false → уведомления копятся в ready
        row = await self.store.get(job_id)
        if row is None or row["status"] != "ready":
            return False
        vacancy = row["vacancy"]
        if row["letter"] is None:
            letter = await self._card(job_id, vacancy, row["content_hash"] or content_hash(vacancy),
                                      await self.store.setting('rules_version'))
            if letter is not None:
                await self.store.set_letter(job_id, letter)
        text = "Нашлась новая релевантная вакансия: %s — %s" % (vacancy.title, vacancy.company or "—")
        return await self.store.notify_ready(job_id, text, row["cluster_id"])

    async def tick(self):
        if await self.store.setting("browser_blocked",False):
            return
        await self.run_pending_rounds()
        if await self.resume_waiting():
            return
        # Сначала переоценка строк, решённых старыми правилами (16.2).
        stale=await self.store.stale_row()
        if stale:
            await self.reassess(stale)
            return
        job_id=await self.store.first_found()
        if job_id:
            await self.prepare(job_id)
            return
        # Уведомления по готовым карточкам (по одной за тик).
        job_id=await self.store.next_ready_without_card()
        if job_id:
            await self.notify_ready(job_id)
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
