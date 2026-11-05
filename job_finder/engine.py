import asyncio
import time
from datetime import datetime
from .models import AttentionRequired, Vacancy, VacancyUnavailable
from .rules import evaluate

# Правило тестов после удаления автоотклика (решение 05.11.2026):
# тест не отклоняет вакансию, а помечается в карточке.
TEST_LABEL = "Тест: потребуется пройти при отклике"


class Engine:
    def __init__(self, config, store, source, writer):
        self.config=config
        self.store=store
        self.source=source
        self.writer=writer

    async def start(self):
        # A local process start is the only user control.
        self.store.set('search_requested',False)
        self.store.set('reconcile_requested',False)
        self.store.set('browser_blocked',False)
        self.store.set('attention_reason','')
        await self.search()

    def attention(self, message):
        self.store.pause(True)
        # Stop work until the user fixes access and restarts locally.
        self.store.set("browser_blocked",True)
        self.store.set("attention_reason",message)
        self.store.enqueue("Нужно действие: "+message+"\nРабота приостановлена.")
        print("Работа остановлена: "+message+". После исправления перезапустите скрипт.")

    async def search(self):
        try:
            print('Поиск вакансий начался.')
            report=await self.source.search(max_pages=self.config.max_search_pages)
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
            vacancy=await self.source.read(self.store.native(job_id))
            self.store.refresh(vacancy)
            decision=evaluate(vacancy)
            reason=decision.reason
            if vacancy.test_required and decision.status in ("ready","needs_clarification"):
                reason += " · " + TEST_LABEL
            if decision.status=="ready":
                letter=await self.writer.generate(vacancy)
                self.store.make_ready(job_id,letter,reason)
            else:
                self.store.decide(job_id,decision.status,reason)
                if decision.status=="needs_clarification":
                    self.store.enqueue("Нужно уточнение: "+vacancy.title+"\n"+vacancy.url+"\n"+reason)
        except VacancyUnavailable as exc:
            self.store.decide(job_id,"needs_clarification",str(exc))
        except AttentionRequired as exc:
            self.attention(str(exc))
        except Exception as exc:
            self.store.decide(job_id,"error","Подготовка не удалась: "+type(exc).__name__)
            self.store.enqueue("Не удалось подготовить вакансию "+self.store.native(job_id)+" ("+type(exc).__name__+"). Повтор возможен вручную.")

    def digest(self, skipped=None):
        labels={"found":"ждут чтения","read":"прочитаны","ready":"готовы","delivered":"доставлены","rejected":"отсеяны","needs_clarification":"уточнение","skipped":"пропущены","error":"ошибки","closed":"закрыты"}
        text="Сводка очереди\n"+"\n".join(labels.get(k,k)+": "+str(v) for k,v in self.store.counts().items())
        cutoff=self.store.setting('last_digest_at','1970-01-01T00:00:00+00:00')
        rows=[row for row in self.store.rows(limit=100000) if row['id']!='hh:137924141']
        text+='\n\nС предыдущей сводки: новых %s, готово %s, отсеяно %s.' % (sum(row['created']>=cutoff for row in rows),sum(row['status']=='ready' and row['updated']>=cutoff for row in rows),sum(row['status']=='rejected' and row['updated']>=cutoff for row in rows))
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
