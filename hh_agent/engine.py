import asyncio
import time
from datetime import datetime
from .models import AttentionRequired, Outcome, Vacancy
from .rules import evaluate


class Engine:
    def __init__(self, config, store, browser, writer):
        self.config=config
        self.store=store
        self.browser=browser
        self.writer=writer

    def attention(self, message):
        self.store.pause(True)
        # Also stop searches until /search or /resume, to avoid hammering an
        # expired login or challenge while only the send queue is paused.
        self.store.set("browser_blocked",True)
        self.store.enqueue("Нужно действие: "+message+"\nОтправка приостановлена.")

    async def search(self):
        try:
            report=await self.browser.search(max_pages=self.config.max_search_pages)
            new=0
            for job_id in report["ids"]:
                new+=int(self.store.discover(Vacancy(job_id,"Вакансия ещё не прочитана","")))
            self.store.set("last_search",time.time())
            self.store.set("search_report",report)
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
            self.store.decide(job_id,decision.status,decision.reason)
            if decision.status=="ready":
                self.store.save_letter(job_id,await self.writer.generate(vacancy))
            elif decision.status=="needs_clarification":
                self.store.enqueue("Нужно уточнение: "+vacancy.title+"\n"+vacancy.url+"\n"+decision.reason)
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
            status="unknown" if self.store.get(job_id)["status"]=="sending" else "error"
            outcome=Outcome(status,reason="Ошибка при отправке: "+type(exc).__name__)
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

    def digest(self):
        labels={"found":"ждут чтения","ready":"готовы","sent":"отправлены","rejected":"отсеяны","needs_clarification":"уточнение","unknown":"результат неизвестен","error":"ошибки","closed":"закрыты","sending":"отправляются"}
        text="Сводка очереди\n"+"\n".join(labels.get(k,k)+": "+str(v) for k,v in self.store.counts().items())
        today=datetime.now().astimezone().date().isoformat()
        rows=self.store.rows(["rejected"],limit=100000)
        skipped=[r for r in rows if "предложено пройти тест" in r["reason"] and datetime.fromisoformat(r["updated"]).astimezone().date().isoformat()==today]
        if skipped:
            text+="\n\nСегодня пропущены из-за теста:\n"+"\n".join(r["vacancy"].url+" — "+r["reason"] for r in skipped)
        return text

    async def tick(self):
        explicit_search=self.store.setting("search_requested",False)
        explicit_reconcile=self.store.setting("reconcile_requested",False)
        if explicit_search or explicit_reconcile:
            self.store.set("browser_blocked",False)
        if self.store.setting("browser_blocked",False):
            return
        if explicit_reconcile:
            self.store.set("reconcile_requested",False)
            await self.reconcile()
            return
        last_search=self.store.setting("last_search",0)
        if explicit_search or (not self.store.is_paused() and time.time()-last_search>=self.config.search_interval):
            self.store.set("search_requested",False)
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
            self.store.enqueue(self.digest())
            self.store.set("digest_day",day)

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(2)
