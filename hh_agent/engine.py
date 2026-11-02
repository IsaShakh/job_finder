import asyncio
import time
from datetime import datetime
from .models import AttentionRequired, Outcome, Vacancy, VacancyUnavailable
from .rules import evaluate


class Engine:
    def __init__(self, config, store, browser, writer):
        self.config=config
        self.store=store
        self.browser=browser
        self.writer=writer

    async def start(self):
        # A local process start is the only user control. Recover an interrupted
        # send before enabling submissions, but never infer that it was unsent.
        self.store.recover()
        self.store.set('search_requested',False)
        self.store.set('reconcile_requested',False)
        self.store.set('browser_blocked',False)
        self.store.set('attention_reason','')
        unknown=bool(self.store.rows(['unknown'],limit=1))
        self.store.pause(unknown)
        if unknown:
            await self.reconcile()
            if self.store.setting('browser_blocked',False):
                return
            self.store.pause(bool(self.store.rows(['unknown'],limit=1)))
        await self.search()

    def attention(self, message):
        self.store.pause(True)
        # Stop browser work until the user fixes access and restarts locally.
        self.store.set("browser_blocked",True)
        self.store.set("attention_reason",message)
        self.store.enqueue("Нужно действие: "+message+"\nОтправка приостановлена.")
        print("Работа с HH остановлена: "+message+". После исправления перезапустите скрипт.")

    async def search(self):
        try:
            print('Поиск вакансий начался.')
            report=await self.browser.search(max_pages=self.config.max_search_pages)
            new=0
            for job_id in report["ids"]:
                new+=int(self.store.discover(Vacancy(job_id,"Вакансия ещё не прочитана","")))
            self.store.set("last_search",time.time())
            self.store.set("search_report",report)
            print('Поиск завершён: найдено %s, новых %s.' % (len(report['ids']),new))
            self.store.enqueue("Поиск завершён: %s ID, новых %s. Страниц: %s.\n%s" % (len(report["ids"]),new,report["pages"],"Охват неполный. "+"; ".join(report["notes"]) if report["incomplete"] else "Все доступные страницы выбранной выдачи обработаны; сайт может менять результаты."))
        except AttentionRequired as exc:
            self.attention(str(exc))
        except Exception as exc:
            self.attention("Не удалось завершить поиск: "+type(exc).__name__)

    async def prepare(self, job_id):
        row=self.store.get(job_id)
        if not row or row["status"]!="found":
            return
        try:
            vacancy=await self.browser.read(job_id)
            self.store.refresh(vacancy)
            if vacancy.already_applied:
                self.store.finish(job_id,"sent","Страница HH подтверждает ранее отправленный отклик")
                return
            decision=evaluate(vacancy)
            if decision.status=="ready":
                letter=await self.writer.generate(vacancy)
                self.store.make_ready(job_id,letter,decision.reason)
            else:
                self.store.decide(job_id,decision.status,decision.reason)
                if decision.status=="needs_clarification":
                    self.store.enqueue("Нужно уточнение: "+vacancy.title+"\n"+vacancy.url+"\n"+decision.reason)
        except VacancyUnavailable as exc:
            self.store.decide(job_id,"needs_clarification",str(exc))
        except AttentionRequired as exc:
            self.attention(str(exc))
        except Exception as exc:
            self.store.decide(job_id,"error","Подготовка не удалась: "+type(exc).__name__)
            self.store.enqueue("Не удалось подготовить вакансию "+job_id+" ("+type(exc).__name__+"). Повтор возможен вручную.")

    def notification(self, job_id, outcome):
        row=self.store.get(job_id)
        vacancy=row["vacancy"]
        text=("Отклик подтверждён: " if outcome.status=="sent" else "Результат требует проверки: ")+vacancy.title+"\n"+vacancy.url
        if outcome.reason:
            text+="\n"+outcome.reason
        if row["letter"]:
            letter=row["letter"]
            text+="\n\nТочное письмо:\n"+letter.text
            text+="\n\nДобавленные утверждения (только для тебя):\n"+("\n".join(letter.added_claims) or "Нет")
            text+="\n\nПодготовка:\n"+("\n".join(letter.preparation) or "Дополнительных тем нет")
        return text

    async def send(self, job_id):
        row=self.store.get(job_id)
        if not self.config.live or self.store.is_paused() or not row or row["status"]!="ready" or not row["letter"]:
            return
        try:
            current=await self.browser.read(job_id)
            self.store.refresh(current)
            if current.already_applied:
                self.store.finish(job_id,"sent","Страница HH подтверждает ранее отправленный отклик")
                return
            decision=evaluate(current)
            self.store.decide(job_id,decision.status,decision.reason)
            if decision.status!="ready":
                if decision.status=="needs_clarification":
                    self.store.enqueue(current.url+"\nУсловия изменились: "+decision.reason)
                return
            if current!=row["vacancy"]:
                self.store.save_letter(job_id,await self.writer.generate(current))
            letter=self.store.get(job_id)["letter"]
            async def gate():
                return self.config.live and self.store.claim(job_id,self.config.resume_id)
            outcome=await self.browser.apply(current,letter,gate)
            if outcome.status=="ready":
                return
            self.store.finish(job_id,outcome.status,outcome.evidence,outcome.reason,
                              self.notification(job_id,outcome) if outcome.status in ("sent","unknown") else None)
            if outcome.status=="unknown":
                self.store.pause(True)
            elif outcome.status in ("needs_clarification","error"):
                self.store.enqueue(current.url+"\n"+outcome.reason)
            self.store.set("next_send_at",time.time()+self.config.send_interval)
        except AttentionRequired as exc:
            self.attention(str(exc))
        except Exception as exc:
            unavailable=isinstance(exc,VacancyUnavailable)
            status="unknown" if self.store.get(job_id)["status"]=="sending" else "needs_clarification" if unavailable else "error"
            outcome=Outcome(status,reason=str(exc) if unavailable else "Ошибка при отправке: "+type(exc).__name__)
            self.store.finish(job_id,status,reason=outcome.reason,notification=self.notification(job_id,outcome))
            if status=="unknown":
                self.store.pause(True)

    async def reconcile(self):
        for row in self.store.rows(["unknown"],limit=100000):
            try:
                outcome=await self.browser.reconcile(row["id"])
                self.store.finish(row["id"],outcome.status,outcome.evidence,outcome.reason,
                                  self.notification(row["id"],outcome))
            except AttentionRequired as exc:
                self.attention(str(exc))
                return
            except Exception as exc:
                self.store.enqueue("Проверка отклика "+row["id"]+" не завершена: "+type(exc).__name__)

    def digest(self, skipped=None):
        labels={"found":"ждут чтения","ready":"готовы","sent":"отправлены","rejected":"отсеяны","needs_clarification":"уточнение","unknown":"результат неизвестен","error":"ошибки","closed":"закрыты","sending":"отправляются"}
        text="Сводка очереди\n"+"\n".join(labels.get(k,k)+": "+str(v) for k,v in self.store.counts().items())
        cutoff=self.store.setting('last_digest_at','1970-01-01T00:00:00+00:00')
        rows=[row for row in self.store.rows(limit=100000) if row['id']!='137924141']
        text+='\n\nС предыдущей сводки: новых %s, подтверждено %s, отсеяно %s.' % (sum(row['created']>=cutoff for row in rows),sum(row['status']=='sent' and row['updated']>=cutoff for row in rows),sum(row['status']=='rejected' and row['updated']>=cutoff for row in rows))
        skipped=self.store.pending_skips() if skipped is None else skipped
        if skipped:
            text+="\n\nПропуски из-за теста с предыдущей сводки:\n"+"\n".join('https://hh.ru/vacancy/'+r['job_id']+" — "+r["reason"] for r in skipped)
        return text

    async def tick(self):
        if self.store.setting("browser_blocked",False):
            return
        last_search=self.store.setting("last_search",0)
        if time.time()-last_search>=self.config.search_interval:
            await self.search()
            return
        if self.config.live and not self.store.is_paused() and time.time()>=self.store.setting("next_send_at",0):
            ready=self.store.rows(["ready"],limit=1)
            if ready:
                await self.send(ready[0]["id"])
                return
        found=self.store.rows(["found"],limit=1)
        if found:
            await self.prepare(found[0]["id"])
        day=datetime.now().astimezone().date().isoformat()
        if self.store.setting("digest_day")!=day:
            skipped=self.store.pending_skips()
            self.store.queue_digest(self.digest(skipped),day,[row['id'] for row in skipped])

    async def run(self):
        await self.start()
        while True:
            await self.tick()
            await asyncio.sleep(2)
