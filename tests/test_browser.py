import os
import unittest
from pathlib import Path
from playwright.async_api import async_playwright
from job_finder.browser import Browser
from job_finder.models import AttentionRequired, Letter, VacancyUnavailable

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH",str(Path(__file__).resolve().parents[1]/".cache/ms-playwright"))
RESUME="b424f687ff113123eb0039ed1f7a496e413267"
DETAIL='''<a href="/applicant/profile/me">Профиль</a><h1 data-qa="vacancy-title">Senior Python</h1>
<div data-qa="vacancy-salary">от 200 000 ₽ за месяц, на руки</div>
<div data-qa="vacancy-description">Python FastAPI для API</div>
<p data-qa="work-formats-text">Формат работы: удалённо</p>
<a data-qa="vacancy-response-link-top" href="/applicant/vacancy_response?vacancyId=123&employerId=456">Откликнуться</a>'''
FORM='''<a href="/applicant/profile/me">Профиль</a><h1>Отклик на вакансию</h1>
<p>Для отклика необходимо ответить на несколько вопросов работодателя</p>
<form id="RESPONSE_MODAL_FORM_ID" onsubmit="event.preventDefault(); document.querySelector('#result').textContent='Вы откликнулись'; document.querySelector('button[type=submit]').textContent='Откликнуться повторно'">
<button type="button" onclick="document.querySelector('#resumes').style.display='block'"><span data-qa="resume-title">Другое резюме</span></button>
<div id="resumes" style="display:none"><label><input type="radio" name="resume" value="other" checked>Другое резюме</label><label><input type="radio" name="resume" value="RESUME">Python Backend разработчик</label></div>
<textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
<button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></form><p id="result"></p>
<script>document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelector('#resumes').style.display='none'})</script>'''.replace("RESUME",RESUME)


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw=await async_playwright().start()
        self.chromium=await self.pw.chromium.launch(headless=True)
        self.context=await self.chromium.new_context()
        self.page=await self.context.new_page()
        self.form=FORM
        self.form_status=200
        self.detail=DETAIL
        self.detail_status=200
        self.submits=0
        await self.page.expose_function("count_submit",self.count_submit)
        async def serve(route):
            url=route.request.url
            html=self.form if "vacancy_response" in url else self.detail
            html += "<script>document.querySelector('form')?.addEventListener('submit',()=>window.count_submit())</script>"
            await route.fulfill(status=self.form_status if "vacancy_response" in url else self.detail_status,content_type="text/html; charset=utf-8",body=html)
        await self.context.route("**/*",serve)
        self.browser=Browser(self.page,RESUME,confirmation_timeout=0.2)

    def count_submit(self):
        self.submits+=1

    async def asyncTearDown(self):
        await self.context.close()
        await self.chromium.close()
        await self.pw.stop()

    async def send(self,allow=True):
        vacancy=await self.browser.read("123")
        async def gate():
            return allow
        return await self.browser.apply(vacancy,Letter("Точное письмо"),gate)

    async def test_success_submits_once_with_exact_resume_and_letter(self):
        result=await self.send()
        self.assertEqual(result.status,"sent")
        self.assertEqual(self.submits,1)
        self.assertEqual(await self.page.locator('input:checked').input_value(),RESUME)
        self.assertEqual(await self.page.locator('textarea').input_value(),"Точное письмо")

    async def test_visible_test_on_vacancy_prevents_form_and_submit(self):
        self.detail+='<p role="alert">Для отклика на вакансию пройдите тест</p>'
        result=await self.send()
        self.assertEqual(result.status,"rejected")
        self.assertEqual(self.submits,0)
        self.assertNotIn("vacancy_response",self.page.url)

    async def test_split_banner_context_and_button_still_skips(self):
        self.detail+='<div><span>Для отклика на вакансию</span><button>Пройти тест</button></div>'
        self.assertEqual((await self.send()).status,"rejected")
        self.assertEqual(self.submits,0)

    async def test_test_scan_survives_page_replacing_loading_elements(self):
        await self.page.goto('https://hh.ru/vacancy/123')
        self.page.set_default_timeout(500)
        await self.page.evaluate('''() => {
            const loading=document.createElement('div');
            loading.innerHTML='<span>Загрузка</span>'.repeat(600);
            document.body.append(loading);
            setTimeout(()=>loading.remove(),150);
        }''')
        self.assertFalse(await self.browser.has_test())
        await self.page.evaluate('''() => document.body.insertAdjacentHTML('beforeend',
            '<div role="alert"><span>Для отклика на вакансию</span><button>Пройти тест</button></div>')''')
        self.assertTrue(await self.browser.has_test())

    async def test_employer_prose_is_not_send_evidence(self):
        self.detail=self.detail.replace('Python FastAPI для API','Python. Если вы откликнулись на нашу вакансию, мы свяжемся с вами. <div>Вы откликнулись</div>')
        vacancy=await self.browser.read('123')
        self.assertFalse(vacancy.already_applied)
        self.assertEqual((await self.browser.reconcile('123')).status,'unknown')

    async def test_deleted_vacancy_is_closed_not_auth_failure(self):
        self.detail_status=404
        self.detail='<h1>Вакансия не найдена</h1>'
        self.assertTrue((await self.browser.read('123')).closed)

    async def test_archived_vacancy_with_collapsed_description_is_skipped(self):
        self.detail=self.detail.replace('data-qa="vacancy-description"',
                                        'data-qa="vacancy-description" style="max-height:0;overflow:hidden"')
        self.detail+='<h2>Вакансия в архиве</h2><button>Показать описание вакансии</button>'
        result=await self.send()
        self.assertEqual(result.status,'closed')
        self.assertEqual(self.submits,0)
        self.assertNotIn('vacancy_response',self.page.url)

    async def test_archived_vacancy_without_description_preserves_existing_response(self):
        self.detail='''<a href="/applicant/profile/me">Профиль</a>
        <h1 data-qa="vacancy-title">Middle data scientist</h1>
        <h2>Вакансия в архиве</h2><button>Показать описание вакансии</button>
        <p>Вы откликнулись</p>'''
        job=await self.browser.read('123')
        self.assertTrue(job.closed)
        self.assertTrue(job.already_applied)
        self.assertEqual((await self.browser.reconcile('123')).status,'sent')
        self.assertEqual(self.submits,0)

    async def test_active_vacancy_still_requires_description(self):
        self.detail='<h1 data-qa="vacancy-title">Senior Python</h1>'
        with self.assertRaisesRegex(AttentionRequired,'Описание вакансии'):
            await self.browser.read('123')

    async def test_archived_page_does_not_hide_access_challenge(self):
        self.detail='''<h1 data-qa="vacancy-title">Senior Python</h1>
        <h2>Вакансия в архиве</h2><input name="captcha">'''
        with self.assertRaisesRegex(AttentionRequired,'HH просит проверку'):
            await self.browser.read('123')

    async def test_specific_vacancy_access_denial_is_local_to_signed_in_account(self):
        self.detail_status=403
        self.detail='''<a href="/applicant/profile/me">Резюме и профиль</a>
        <p>Вам недоступна эта вакансия. Войдите как пользователь,
        у которого есть доступ на просмотр, либо как работодатель, создавший эту вакансию.</p>'''
        with self.assertRaisesRegex(VacancyUnavailable,'недоступна'):
            await self.browser.read('123')
        self.assertEqual(self.submits,0)

    async def test_unavailable_vacancy_during_apply_reread_keeps_local_reason(self):
        job=await self.browser.read('123')
        await self.page.goto('https://hh.ru/')
        self.detail_status=403
        self.detail='''<a href="/applicant/profile/me">Профиль</a>
        <p>Вам недоступна эта вакансия.</p>'''
        async def gate():
            self.fail('Unavailable vacancy must not start a submission')
        with self.assertRaisesRegex(VacancyUnavailable,'недоступна'):
            await self.browser.apply(job,Letter('Письмо'),gate)
        self.assertEqual(self.submits,0)

    async def test_generic_403_still_blocks_browser_work(self):
        self.detail_status=403
        self.detail='<h1>Forbidden</h1>'
        with self.assertRaisesRegex(AttentionRequired,'403'):
            await self.browser.read('123')

    async def test_vacancy_denial_without_confirmed_login_requires_attention(self):
        self.detail_status=403
        self.detail='<p>Вам недоступна эта вакансия.</p>'
        with self.assertRaisesRegex(AttentionRequired,'вход'):
            await self.browser.read('123')

    async def test_vacancy_denial_cannot_hide_captcha(self):
        self.detail_status=403
        self.detail='''<a href="/applicant/profile/me">Профиль</a>
        <p>Вам недоступна эта вакансия.</p><input name="captcha">'''
        with self.assertRaisesRegex(AttentionRequired,'HH просит проверку'):
            await self.browser.read('123')

    async def test_response_form_403_is_not_treated_as_unavailable_vacancy(self):
        self.form_status=403
        self.form='''<a href="/applicant/profile/me">Профиль</a>
        <p>Вам недоступна эта вакансия.</p>'''
        with self.assertRaisesRegex(AttentionRequired,'403'):
            await self.send()
        self.assertEqual(self.submits,0)

    async def test_new_form_test_is_skipped(self):
        self.form=self.form.replace('<textarea','<p>Пройдите тест работодателя</p><textarea')
        self.assertEqual((await self.send()).status,"rejected")
        self.assertEqual(self.submits,0)

    async def test_test_step_without_response_form_is_skipped(self):
        self.form='<a href="/applicant/profile/me">Профиль</a><p role="alert">Пройдите тест</p>'
        self.assertEqual((await self.send()).status,"rejected")
        self.assertEqual(self.submits,0)

    async def test_python_in_key_skills_is_included(self):
        self.detail=self.detail.replace('Senior Python','Senior Backend').replace('Python FastAPI для API','Создание сервисов')
        self.detail+='<ul><li data-qa="skills-element">Python</li><li data-qa="skills-element">SQL</li></ul>'
        job=await self.browser.read('123')
        self.assertIn('Python',job.requirements)

    async def test_radio_questionnaire_cannot_be_silently_ignored(self):
        self.form=self.form.replace('<textarea','<p>Есть право на работу?</p><input type="radio" name="permit" value="yes"><input type="radio" name="permit" value="no"><textarea')
        self.assertEqual((await self.send()).status,"needs_clarification")
        self.assertEqual(self.submits,0)

    async def test_hidden_clipped_and_generic_ad_do_not_block(self):
        self.detail+='<aside>Пройдите тест навыков hh.ru</aside>'
        self.form=self.form.replace('<textarea','<div style="max-height:0;overflow:hidden"><p>Для отклика пройдите тест</p></div><textarea')
        self.assertEqual((await self.send()).status,"sent")

    async def test_questionnaire_is_not_a_test_but_requires_user(self):
        self.form=self.form.replace('<textarea','<label>Когда готовы выйти?<input name="start_date" required></label><textarea')
        self.assertEqual((await self.send()).status,"needs_clarification")
        self.assertEqual(self.submits,0)

    async def test_pause_gate_prevents_final_click(self):
        self.assertEqual((await self.send(False)).status,"ready")
        self.assertEqual(self.submits,0)

    async def test_unknown_result_has_no_automatic_retry(self):
        self.form=self.form.replace("document.querySelector('#result').textContent='Вы откликнулись'","document.querySelector('#result').textContent='Обработка'")
        self.form=self.form.replace("document.querySelector('button[type=submit]').textContent='Откликнуться повторно'", "void(0)")
        self.assertEqual((await self.send()).status,"unknown")
        self.assertEqual(self.submits,1)

    async def test_existing_response_wins_over_test(self):
        self.detail+='<p>Вы откликнулись</p><p role="alert">Для отклика пройдите тест</p>'
        self.assertEqual((await self.send()).status,"sent")
        self.assertEqual(self.submits,0)

    async def test_missing_exact_resume_never_uses_similar_title(self):
        self.form=self.form.replace(RESUME,"wrong")
        self.assertEqual((await self.send()).status,"needs_clarification")
        self.assertEqual(self.submits,0)

    async def test_search_uses_all_pages_and_deduplicates(self):
        await self.context.unroute("**/*")
        async def serve(route):
            if "page=1" in route.request.url:
                html='<h1 data-qa="vacancies-search-header">Найдено 2 вакансии</h1><a data-qa="serp-item__title" href="/vacancy/456">Python</a>'
            else:
                html='<h1 data-qa="vacancies-search-header">Найдено 2 вакансии</h1><a data-qa="serp-item__title" href="/vacancy/123">Python</a><a data-qa="pager-next" href="/search/vacancy?text=Python&page=1">Далее</a>'
            await route.fulfill(status=200,content_type="text/html; charset=utf-8",body=html)
        await self.context.route("**/*",serve)
        result=await self.browser.search(max_pages=5)
        self.assertEqual(set(result["ids"]),{"123","456"})
        self.assertFalse(result["incomplete"])
