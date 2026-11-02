# Программный доступ к API hh.ru вместо браузера

Дата: 30 октября 2026 года. Решение принято пользователем: перевести агента с «человекоподобного» браузерного обхода на программный HTTP-API.

## Вывод

Соискательский API hh.ru **технически жив**: он обслуживает официальное Android-приложение и доступен программно через OAuth2 с ключами этого приложения. Поддерживаются поиск, чтение вакансий, отправка откликов (`POST /negotiations`), список откликов и переписка. Браузер для основного конвейера (поиск → чтение → отклик → подтверждение) больше не нужен.

## Что было проверено

1. **Анонимный публичный API** (`api.hh.ru/vacancies` без токена) — по-прежнему `403 forbidden` из нашего окружения (проверено 30.10.2026 с заголовком User-Agent). Справочники (`/dictionaries`) — 200. Метод поиска документирован в [официальных доках](https://github.com/hhru/api/blob/master/docs/vacancies.md), но анонимно недоступен. Не используется.
2. **Эталонная реализация** — [hh-applicant-tool](https://pypi.org/project/hh-applicant-tool/) ([GitHub](https://github.com/s3rgeym/hh-applicant-tool), версия 2.5.x, активно поддерживается в 2026) работает полностью без браузера (автор: «не использует Headless Chromium… используется лишь стандартная библиотека requests»). Протокол взят из её исходников и из сохранённой автором [архивной OpenAPI-документации соискательского API](https://github.com/s3rgeym/hh-applicant-tool/blob/main/docs/hhapi/openapi.yml).
3. Код эталона **не копируется** (лицензия limited non-commercial, без публикации изменений) — он используется только как источник знаний о протоколе; реализация написана заново в `job_finder/hh.py`.

## Протокол (кратко)

### Авторизация (OAuth2 мобильного приложения)

1. `GET https://hh.ru/oauth/authorize?client_id=…&response_type=code&force_login=true&role=applicant&force_role=true&skip_choose_account=true` → 302 на страницу входа. В HTML страницы `<template id="HH-Lux-InitialState">` содержит JSON с `xsrfToken` и `loginTrustFlagsSecret` (публичный RSA-ключ), в query — `backurl`.
2. Вход на сайте (XHR с заголовками `X-XSRFToken`, `X-Requested-With: XMLHttpRequest`, `X-Hhtmfrom`/`X-Hhtmsource`):
   - по паролю: `POST <login_url>` (form: `accountType=APPLICANT`, `failUrl`, `remember`, `loginTrustFlags`, `username`, `password`, `captchaText`);
   - по коду: `POST /account/otp_generate` (`otpType=phone|email`, `operationType=applicant_otp_auth`) → `POST /account/login/by_code` (`username`, `code`, `backurl`).
   - `loginTrustFlags` = base64(RSA-OAEP-SHA256(`{...данные... ,"ts":ms}`)) публичным ключом страницы.
   - Капча: ответ содержит `hhcaptcha.captchaState` → `POST /captcha?lang=RU` → `captchaKey` → `GET /captcha/picture?key=` → ввод текста → повтор исходного POST с `captchaState/captchaKey/captchaText`.
3. `GET backurl` (Referer = страница входа) → 302 `Location: hhandroid://oauthresponse?code=…`.
4. `POST https://hh.ru/oauth/token` (`client_id`, `client_secret`, `code`, `grant_type=authorization_code`) → `access_token` (начинается с `USER`, живёт ~2 недели), `refresh_token` (одноразовый), `expires_in`. Обновление: `grant_type=refresh_token`.

Ключи приложения — публичные константы из декомпилированного Android-приложения (в [constants.py](https://github.com/s3rgeym/hh-applicant-tool/blob/main/src/hh_applicant_tool/constants.py) эталона): client_id `HIOMIAS39…`, client_secret `V9M870DE…`.

### Запросы к API (`https://api.hh.ru`)

Заголовки: `Accept: application/json`, `X-Force-App-Access: true`, `X-HH-App-Active: true`, `Authorization: Bearer USER…`, мобильный User-Agent.

| Операция | Запрос |
| --- | --- |
| Поиск | `GET /vacancies?text&schedule&order_by&experience&per_page=100&page=N` (лимит 2000 результатов) |
| Рекомендации | `GET /resumes/{id}/similar_vacancies` |
| Чтение вакансии | `GET /vacancies/{id}` — `description` (HTML), `key_skills`, `has_test`, `archived`, `relations` (отклик/отказ), `salary{from,to,currency,gross}`, `work_format`, `schedule`, `response_url` |
| Отклик | `POST /negotiations` (form: `resume_id`, `vacancy_id`, `message`); успех — пустое тело `{}`; редирект — вакансия требует форму |
| Список откликов | `GET /negotiations?per_page=100&page=N` — подтверждение факта отклика |
| Профиль | `GET /me`, `GET /resumes/mine` |
| Чёрный список | `PUT /vacancies/blacklisted/{id}` |

Ошибки: 403 `forbidden` (в т.ч. истёкший токен), `captcha_required` с `captcha_url`; при превышении лимита откликов — `negotiations-limit-exceeded`.

### Ограничения и антибот (по эталону)

- Пауза ≥0.345 с между запросами; 1–3 с перед откликом; задержка 5–10 с на шаге входа.
- Лимиты HH: ~2000 показанных вакансий/сутки, ~200 откликов/сутки.
- Капча возможна при агрессивном поведении; решается только человеком (в нашем проекте — по правилам).

## Риски и смягчение

| Риск | Смягчение в проекте |
| --- | --- |
| Нарушение правил hh.ru (автоматические отклики запрещены) | Режим просмотра по умолчанию; скромные лимиты (≤50 откликов/день); паузы как у человека; личное использование одного аккаунта |
| HH меняет ключи/эндпоинты | API-клиент изолирован в одном модуле; ошибки → пауза с понятной причиной; браузерный путь остаётся как резерв |
| Капча | Останавливаем работу (`AttentionRequired`), человек решает вручную; при входе — капча в терминале |
| Бан/теневой бан аккаунта | Консервативные интервалы (SEARCH_INTERVAL, SEND_INTERVAL), никаких массовых рассылок, человеческий темп откликов |
| Дубли откликов | Прежние инварианты: `claim` до отправки, подтверждение по списку откликов, `unknown` без автоповтора |

## Решение по архитектуре

1. Новый модуль `job_finder/hh.py`: OAuth-авторизация, клиент API (search/read/apply/reconcile), интерфейс совместим с текущим `Browser` — Engine не меняет логику.
2. CLI: новая команда `api-login` (пароль или код из SMS/почты, капча в терминале); `doctor` проверяет наличие токена.
3. Режим запуска: если существует `.state/api-token.json` — работа через API; иначе — прежний браузерный путь (резерв). Команда `login` (браузер) сохранена.
4. Отклик: `POST /negotiations`; подтверждение — наличие вакансии в `GET /negotiations`; не найдено → `unknown` (проверка вручную, автоповтор запрещён). Редирект на форму (анкеты/тесты) → `needs_clarification` для ручной обработки, как в браузерном сценарии.
5. Инварианты проекта (claim, одноразовая отправка, подтверждение площадкой, санитизация ошибок) сохранены без изменений.
