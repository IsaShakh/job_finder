"""Only public UI navigation; no private HH endpoints or cookie export."""
import asyncio
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from .models import AttentionRequired, Outcome, Vacancy
from .rules import evaluate, parse_salary

# Read rendered text, including off-screen content, but excluding text clipped by
# collapsed ancestors. Playwright's is_visible alone does not check that clipping.
VISIBLE_TEXT = r'''(root) => {
 const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT); const out=[];
 while(walker.nextNode()) {
  const n=walker.currentNode; if(!n.textContent.trim()) continue;
  if(n.parentElement.closest('script,style,noscript,template')) continue;
  const range=document.createRange();range.selectNodeContents(n);
  let r=range.getBoundingClientRect(); let box={left:r.left,right:r.right,top:r.top,bottom:r.bottom};
  let ok=r.width>0&&r.height>0;
  for(let e=n.parentElement;e&&ok;e=e.parentElement) {
   const s=getComputedStyle(e);
   if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0) {ok=false;break;}
   const b=e.getBoundingClientRect();
   if(/hidden|clip|auto|scroll/.test(s.overflowY)&&e!==document.body&&e!==document.documentElement) {
    box.top=Math.max(box.top,b.top);box.bottom=Math.min(box.bottom,b.bottom);
   }
   if(/hidden|clip|auto|scroll/.test(s.overflowX)&&e!==document.body&&e!==document.documentElement) {
    box.left=Math.max(box.left,b.left);box.right=Math.min(box.right,b.right);
   }
   if(box.bottom<=box.top||box.right<=box.left) ok=false;
  }
  if(ok) out.push(n.textContent.trim());
 }
 return out.join(' ').replace(/\s+/g,' ').trim();
}'''
TEST_RE = re.compile(r"(?:прой(?:ти|дите)|пройти|выполн(?:ить|ите)|начать|начните|пройти\s+онлайн).{0,90}(?:тест|задани)|(?:пройти|проходить).{0,35}тестирование",re.I)
SENT_RE = re.compile(r"вы (?:уже )?отклик(?:нулись|ались)|отклик (?:уже )?отправлен|откликнуться повторно",re.I)


def safe_hh_url(url):
    parsed=urlparse(url)
    if parsed.scheme!="https" or not parsed.hostname or not (parsed.hostname=="hh.ru" or parsed.hostname.endswith(".hh.ru")) or parsed.username or parsed.password or parsed.port not in (None,443):
        raise AttentionRequired("Неожиданный адрес страницы HH; требуется проверка")
    return url


class Browser:
    def __init__(self, page, resume_id, confirmation_timeout=20):
        self.page=page
        self.resume_id=resume_id
        self.confirmation_timeout=confirmation_timeout

    async def text(self, selector="body"):
        locator=self.page.locator(selector)
        if not await locator.count():
            return ""
        return await locator.first.evaluate(VISIBLE_TEXT)

    async def goto(self, url):
        response=await self.page.goto(safe_hh_url(url),wait_until="domcontentloaded",timeout=45000)
        safe_hh_url(self.page.url)
        if response and response.status>=400:
            raise AttentionRequired("HH вернул ошибку страницы (код %s)" % response.status)
        await self.check_access()

    async def check_access(self, require_auth=False):
        safe_hh_url(self.page.url)
        text=await self.text()
        if re.search(r"подтвердите.{0,25}(?:человек|робот)|слишком много запросов|доступ (?:временно )?ограничен",text,re.I) or await self.page.locator('input[name="captcha"],iframe[src*="captcha"]').count():
            raise AttentionRequired("HH просит проверку или ограничил доступ. Откройте вход вручную")
        if re.search(r"/(?:account/login|login|account/signup)",self.page.url):
            raise AttentionRequired("Нужно заново войти в HH")
        if require_auth and not await self.page.locator('a[href*="/applicant/profile/me"]').count():
            raise AttentionRequired("Не удалось подтвердить вход в аккаунт HH")

    async def sent_evidence(self):
        text=await self.text()
        match=SENT_RE.search(text)
        return (match.group(0)+" · "+self.page.url) if match else ""

    async def has_test(self):
        form=await self.text('#RESPONSE_MODAL_FORM_ID')
        if TEST_RE.search(form):
            return True
        # Vacancy-specific banner outside the form; unrelated site skill ads
        # do not qualify. Inspect local blocks, never concatenate the whole page.
        candidates=self.page.locator('p,[role="alert"],[data-qa*="test"],a,button')
        for node in await candidates.all():
            if await node.evaluate("e=>!!e.closest('footer,header,nav,aside')"):
                continue
            text=await node.evaluate(VISIBLE_TEXT)
            on_response=urlparse(self.page.url).path=="/applicant/vacancy_response"
            if len(text)<900 and TEST_RE.search(text) and (on_response or re.search(r"ваканси|отклик|работодател",text,re.I)):
                return True
        return False

    async def read(self, job_id):
        if not str(job_id).isdigit():
            raise ValueError("Некорректный ID вакансии")
        await self.goto("https://hh.ru/vacancy/"+str(job_id))
        # Wait for real content, not a SPA skeleton, with a bounded timeout.
        try:
            await self.page.locator('[data-qa="vacancy-title"]').wait_for(timeout=10000)
        except Exception:
            text=await self.text()
            if re.search(r"вакансия (?:в архиве|закрыта|удалена)|вакансия не найдена",text,re.I):
                return Vacancy(str(job_id),"Закрытая вакансия","",closed=True)
            raise AttentionRequired("Не удалось прочитать вакансию: структура страницы изменилась") from None
        title=await self.text('[data-qa="vacancy-title"]')
        description=await self.text('[data-qa="vacancy-description"]')
        if not description:
            raise AttentionRequired("Описание вакансии не загрузилось")
        salary=await self.text('[data-qa="vacancy-salary"]')
        work=await self.text('[data-qa="work-formats-text"]')
        remote=bool(re.search(r"удал[её]н|remote",work,re.I)) if work else None
        skills=" ".join([await node.evaluate(VISIBLE_TEXT) for node in await self.page.locator('[data-qa="skills-element"]').all()])
        return Vacancy(str(job_id),title,description,company=await self.text('[data-qa="vacancy-company-name"]'),salary=parse_salary(salary) if salary else None,remote=remote,test_required=await self.has_test(),already_applied=bool(await self.sent_evidence()),closed=bool(re.search(r"вакансия (?:в архиве|закрыта)",await self.text(),re.I)),requirements=skills)

    async def search(self, max_pages=100):
        ids=set()
        report={"ids":[],"pages":0,"incomplete":False,"notes":[]}
        partitions=[None]
        split=False
        while partitions:
            experience=partitions.pop(0)
            query={"text":"Python","schedule":"remote","order_by":"publication_time"}
            if experience:
                query["experience"]=experience
            url="https://hh.ru/search/vacancy?"+urlencode(query)
            visited=set()
            count=0
            expected=None
            partition_ids=set()
            capped=False
            while url and count<max_pages:
                if url in visited:
                    capped=True
                    break
                visited.add(url)
                await self.goto(url)
                await self.page.locator('[data-qa="vacancies-search-header"]').wait_for(timeout=15000)
                header=await self.text('[data-qa="vacancies-search-header"]')
                match=re.search(r"(?:найдено|нашли)\s*([\d\s\u00a0\u202f]+)",header,re.I)
                if expected is None and match:
                    expected=int(re.sub(r"\D","",match.group(1)))
                links=await self.page.locator('a[data-qa="serp-item__title"]').evaluate_all("es=>es.map(e=>e.href)")
                for link in links:
                    parsed=urlparse(safe_hh_url(link))
                    match=re.fullmatch(r"/vacancy/(\d+)",parsed.path)
                    if match:
                        partition_ids.add(match.group(1))
                count+=1
                report["pages"]+=1
                next_link=self.page.locator('a[data-qa="pager-next"]')
                url=urljoin(self.page.url,await next_link.first.get_attribute('href')) if await next_link.count() else None
                if url and urlparse(url).path!="/search/vacancy":
                    raise AttentionRequired("Неожиданный переход из поиска")
            ids.update(partition_ids)
            capped=capped or bool(url) or (expected is not None and len(partition_ids)<expected)
            if capped and experience is None and not split:
                split=True
                partitions.extend(["noExperience","between1And3","between3And6","moreThan6"])
                report["notes"].append("Выдача ограничена или менялась; дополнительно обходим четыре диапазона опыта")
            elif capped:
                report["incomplete"]=True
                report["notes"].append("Не вся выдача доступна: "+str(experience or "общий поиск"))
        report["ids"]=sorted(ids,key=int)
        return report

    async def apply(self, vacancy, letter, before_send):
        attempted=False
        try:
            # The worker just re-read this exact vacancy; never trust a form left
            # over from a previous task or another vacancy.
            if urlparse(self.page.url).path!="/vacancy/"+vacancy.id:
                vacancy=await self.read(vacancy.id)
            await self.check_access(require_auth=True)
            evidence=await self.sent_evidence()
            if evidence:
                return Outcome("sent",evidence,"Ранее отправленный отклик")
            decision=evaluate(vacancy)
            if decision.status!="ready":
                return Outcome(decision.status,reason=decision.reason)
            if await self.has_test():
                return Outcome("rejected",reason="Пропущена: предложено пройти тест")
            links=self.page.locator('a[data-qa="vacancy-response-link-top"]')
            if not await links.count():
                return Outcome("needs_clarification",reason="Нет подтверждённого перехода к форме отклика")
            href=await links.first.get_attribute('href')
            response_url=urljoin(self.page.url,href or "")
            parsed=urlparse(safe_hh_url(response_url))
            if parsed.path!="/applicant/vacancy_response" or parse_qs(parsed.query).get("vacancyId")!=[vacancy.id]:
                return Outcome("needs_clarification",reason="Переход к форме не соответствует вакансии")
            # GET navigation to the observed full form, never click the quick
            # response button which may submit immediately on some HH pages.
            await self.goto(response_url)
            evidence=await self.sent_evidence()
            if evidence:
                return Outcome("sent",evidence)
            if await self.has_test():
                return Outcome("rejected",reason="Пропущена: предложено пройти тест")
            await self.page.locator('#RESPONSE_MODAL_FORM_ID').wait_for(timeout=15000)
            if await self.text('[data-qa="hidden-resume-warning"]'):
                return Outcome("needs_clarification",reason="HH требует изменить видимость резюме")
            selected=await self.select_resume()
            if not selected:
                return Outcome("needs_clarification",reason="Не удалось выбрать и проверить нужное резюме")
            letter_input=self.page.locator('[data-qa="vacancy-response-popup-form-letter-input"]')
            if not await letter_input.count():
                add=self.page.get_by_role("button",name=re.compile(r"Сопроводительное письмо.*Добавить",re.I))
                if await add.count()!=1:
                    return Outcome("needs_clarification",reason="Не найдено поле сопроводительного письма")
                await add.click(timeout=5000)
            await letter_input.fill(letter.text,timeout=5000)
            # Unknown questionnaire answers are never fabricated. Inspect all
            # visible extra controls, not just required fields.
            controls=self.page.locator('#RESPONSE_MODAL_FORM_ID input:not([type=hidden]),#RESPONSE_MODAL_FORM_ID select,#RESPONSE_MODAL_FORM_ID textarea:not([data-qa="vacancy-response-popup-form-letter-input"])')
            for control in await controls.all():
                if await control.is_visible():
                    return Outcome("needs_clarification",reason="В анкете есть дополнительные вопросы работодателя")
            if await self.has_test():
                return Outcome("rejected",reason="Пропущена: предложено пройти тест")
            await self.check_access(require_auth=True)
            if parse_qs(urlparse(self.page.url).query).get("vacancyId")!=[vacancy.id]:
                raise AttentionRequired("Форма перешла на другую вакансию")
            submit=self.page.locator('[data-qa="vacancy-response-submit-popup"]')
            if await submit.count()!=1 or not await submit.is_enabled() or (await submit.inner_text()).strip()!="Откликнуться":
                return Outcome("needs_clarification",reason="Кнопка отправки недоступна или изменилась")
            await submit.click(trial=True,timeout=5000)
            if await self.has_test():
                return Outcome("rejected",reason="Пропущена: предложено пройти тест")
            if not await before_send():
                return Outcome("ready",reason="Пауза или режим просмотра: отправка не начата")
            # Durable intent exists before this line. Any subsequent error has
            # unknown outcome, including a click timeout.
            attempted=True
            await submit.click(timeout=10000,no_wait_after=True)
            deadline=time.monotonic()+self.confirmation_timeout
            while time.monotonic()<deadline:
                evidence=await self.sent_evidence()
                if evidence:
                    return Outcome("sent",evidence)
                await self.check_access()
                await asyncio.sleep(0.1)
            return Outcome("unknown",reason="HH не подтвердил отправку. Требуется проверка истории")
        except AttentionRequired:
            if attempted:
                return Outcome("unknown",reason="После начала отправки HH запросил действие пользователя")
            raise
        except Exception as exc:
            # Exception messages can contain page contents or request URLs.
            return Outcome("unknown" if attempted else "error",reason="Сбой браузера: "+type(exc).__name__)

    async def select_resume(self):
        title=self.page.locator('#RESPONSE_MODAL_FORM_ID [data-qa="resume-title"]')
        if await title.count()!=1:
            return False
        await title.click(timeout=5000)
        radio=self.page.locator('input[type="radio"][value="'+self.resume_id+'"]')
        if await radio.count()!=1:
            await self.page.keyboard.press("Escape")
            return False
        if not await radio.is_checked():
            await radio.check(timeout=5000)
            # Real HH closes its menu after changing the selection. Reopen it
            # if necessary and verify the exact checked ID, never a title match.
            if not await radio.count() or not await radio.is_visible():
                await title.click(timeout=5000)
        checked=await radio.is_checked()
        await self.page.keyboard.press("Escape")
        return checked

    async def reconcile(self, job_id):
        await self.read(job_id)
        evidence=await self.sent_evidence()
        if evidence:
            return Outcome("sent",evidence,"Подтверждён ранее отправленный отклик")
        # Absence of a badge cannot prove that a send failed. Never clear the
        # unknown state from absence alone or requeue automatically.
        return Outcome("unknown",reason="На странице нет подтверждения. Проверьте историю откликов HH вручную")
