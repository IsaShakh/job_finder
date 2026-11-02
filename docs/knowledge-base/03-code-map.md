# 03. Карта кода (файл за файлом)

Все пути относительно корня проекта. Порядок — от «словаря» к оркестратору.

## hh_agent/models.py (73 строки)

Типы данных и исключения.

- `Salary` — `minimum`, `maximum`, `currency` (по умолч. RUB), `period` (по умолч. month), `raw` (исходный текст).
- `Vacancy` — `id`, `title`, `description`, `company`, `salary`, `remote` (bool/None), `test_required`, `closed`, `already_applied`, `requirements` (текст ключевых навыков). Свойство `url` строит ссылку только из числового ID (валидация); `to_dict`/`from_dict` для JSON в БД.
- `Decision` — `status`, `reason` (результат `evaluate`).
- `Letter` — `text`, `fact_ids`, `added_claims`, `preparation`.
- `Outcome` — `status`, `evidence`, `reason` (результат отправки/сверки).
- `AttentionRequired(Exception)` — капча, выход из аккаунта, смена структуры сайта: нужно действие человека.
- `VacancyUnavailable(Exception)` — HH явно отказывает в доступе к одной вакансии (не блокирует остальную работу).

## hh_agent/config.py (81 строка)

- Константа `RESUME_ID = 'b424f687ff113123eb0039ed1f7a496e413267'`.
- `Config` (frozen dataclass): пути (`root`, `.state/`, `agent.sqlite3`, `browser-profile`, `api-token.json`) и настройки. `telegram_token` и `llm_api_key` — `repr=False` (не светятся в repr/логах).
- `Config.load(root)` — `.env` + переменные окружения (окружение побеждает).
- Строгая валидация: формат токена Telegram (`^\d+:[A-Za-z0-9_-]+$`), положительные целые для интервалов, `LLM_BASE_URL` — HTTPS без пользователя/пароля/параметров, `HH_RESUME_ID` обязан совпадать с согласованным. Ошибки валидации не печатают сами секреты.
- Значения по умолчанию: `LLM_BASE_URL=https://api.openai.com/v1`, `live=false`, `headless=false`, `search_interval=21600` (6 ч), `send_interval=90` с, `max_search_pages=100`.

## hh_agent/rules.py (64 строки)

Чистые функции, без I/O (легко тестируются).

- `parse_salary(raw)` — разбор строки зарплаты: числа, валюта (₽/$/€/₸ → RUB/USD/EUR/KZT), период (месяц/час/год/день), «от/до» для одного числа. Неоднозначный текст → `minimum=None, maximum=None` (без угадывания).
- `evaluate(vacancy) → Decision` — все правила отбора (детально в [05-business-rules.md](05-business-rules.md)): закрыта → `closed`; тест → `rejected`; Python; удалёнка; командировки; география; грейд; зарплата (порог 150 000 ₽).

## hh_agent/store.py (193 строки)

SQLite-слой. Схема и статусы — в [04-data-model.md](04-data-model.md). Ключевые методы:

- `discover(vacancy)` — `INSERT OR IGNORE` (защита от дублей из поиска).
- `refresh(vacancy)` — обновить снимок вакансии в `data`.
- `decide(job_id, status, reason)` — смена статуса; финальные статусы (`sent/sending/unknown`) не перезаписываются; пропуск теста пишется в `skip_events`.
- `make_ready(job_id, letter, reason)` — письмо и статус `ready` сохраняются **вместе** (атомарно), только из `found`.
- `save_letter` — заменить письмо при изменившихся условиях.
- `claim(job_id, resume_id)` — атомарный перевод `ready → sending` + запись в `attempts`; возвращает False при паузе.
- `finish(job_id, status, evidence, reason, notification)` — финализация; `sent` требует непустого `evidence`; из `sending/unknown` можно выйти только в `sent/unknown` (иначе принудительно `unknown` — «отправка начата; нужно проверить историю»); закрывает попытку в `attempts`; кладёт уведомление в outbox.
- `recover()` — при запуске: `sending → unknown`, незакрытые попытки → `unknown`, `ready` без письма → `found`.
- `rows(statuses, limit, offset, newest)`, `counts()`, `get(job_id)`.
- `pause/is_paused/setting/set` — ключ-значение в `settings` (JSON).
- `pending_skips()`, `queue_digest()` — учёт пропусков тестов и ежедневной сводки.
- `command(update_id, action, response)` — дедупликация Telegram-обновлений через `inbox`, продвижение `telegram_offset`.
- `enqueue/pending_messages/delivered` — outbox уведомлений.

## hh_agent/hhapi.py (~600 строк)

**Программный источник вакансий (основной)** — API hh.ru в стиле официального Android-приложения. Протокол: [ресёрч](../research/2026-10-30-hh-programmatic-api.md).

- Константы: ключи Android-приложения (`ANDROID_CLIENT_ID/SECRET` — публичные константы приложения, не наши секреты), User-Agent'ы, лимиты (пауза ≥0.4 с, 100 вакансий/страница, 2000 результатов, задержка 1–3 с перед откликом).
- `TokenStore` — `.state/api-token.json` (0600, атомарная запись, clear).
- `vacancy_from_json(data)` — JSON API → `models.Vacancy`: зарплата из `salary{from,to,currency,gross}` (RUR→RUB), `work_format`→remote, `has_test`→test_required, `archived`/type→closed, `relations`→already_applied, `key_skills`→requirements, описание без HTML.
- `HHClient` — интерфейс совместим с `Browser` (Engine не меняется):
  - `search(max_pages)` — пагинация `GET /vacancies` (per_page=100), разбиение по 4 диапазонам опыта при ограничении лимитом API, отчёт `{ids, pages, incomplete, notes}`;
  - `read(job_id)` — `GET /vacancies/{id}`; 404/410 → закрытая вакансия;
  - `apply(vacancy, letter, before_send)` — повторное чтение (relations), повторная оценка правил, gate=claim перед отправкой, `POST /negotiations` (form: resume_id, vacancy_id, message); успех → подтверждение поиском вакансии в `GET /negotiations` (не найдено → `unknown`); редирект на форму → `needs_clarification`; капча после отправки → `unknown`; лимит откликов → `error`;
  - `reconcile(job_id)` — проверка по списку откликов;
  - автопродление токена: 403 с истёкшим токеном → один `POST /oauth/token` (refresh) → повтор запроса; капча API → `AttentionRequired`.
- Авторизация `authorize(config, username, password, otp_code, captcha_solve, …)`: GET `/oauth/authorize` (роль applicant, force_login) → страница входа → `parse_initial_state` (шаблон `HH-Lux-InitialState`: `xsrfToken`, `loginTrustFlagsSecret`) → вход паролем (`POST` на страницу входа) или кодом (`POST /account/otp_generate` → `POST /account/login/by_code`); `trust_flags()` — RSA-OAEP-SHA256 `{"…","ts":ms}` публичным ключом страницы; капча — `POST /captcha?lang=RU` → картинка → повтор с `captchaState/captchaKey/captchaText`; затем `GET backurl` → 302 `hhandroid://oauthresponse?code=…` → `POST /oauth/token` → токены в TokenStore.
- Все ошибки санитизированы (`HHAPIError`, `AttentionRequired`): токены и тела ответов в исключения не попадают.

## hh_agent/browser.py (318 строк)

**Запасной браузерный адаптер hh.ru** (используется, когда нет токена API, и для команды `login`). Только публичные страницы; адреса проверяются `safe_hh_url` (только `https://*.hh.ru`, без credentials).

- `VISIBLE_TEXT` — JS-фрагмент: читает видимый текст с учётом клиппинга/скрытых предков (Playwright `is_visible` этого не ловит).
- `TEST_BLOCKS`/`TEST_RE` — детектор баннеров «пройдите тест» с учётом разделённых текст+кнопка, но без ложных срабатываний на рекламу общих тестов.
- `SENT_RE` — шаблоны подтверждения отклика («Вы откликнулись», «отклик уже отправлен», «откликнуться повторно»).
- `goto(url)` — навигация с проверкой доступа; 403 на странице вакансии с текстом «Вам недоступна эта вакансия» → `VacancyUnavailable` (локальная проблема одной вакансии), прочие ошибки → `AttentionRequired`.
- `check_access(require_auth)` — детект капчи/блокировки/страницы входа; при `require_auth` — наличие ссылки на профиль соискателя.
- `read(job_id) → Vacancy` — чтение полей по селекторам `data-qa`: заголовок, описание, зарплата, формат работы, компания, навыки; распознавание закрытых/архивных вакансий (404/410, «в архиве/закрыта», свёрнутое описание).
- `search(max_pages)` — постраничный сбор ID из выдачи; при ограничении — разбиение по 4 диапазонам опыта; возвращает отчёт `{ids, pages, incomplete, notes}`.
- `has_test()` — проверка теста на странице вакансии и в форме (видимые блоки, локальный контекст; скрытые/рекламные не считаются).
- `apply(vacancy, letter, before_send) → Outcome` — полный сценарий отправки с многократными проверками (детали в [02-architecture.md](02-architecture.md)); `before_send` — gate `claim` прямо перед кликом.
- `select_resume()` — выбор резюме по ID radio-поля, с повторной проверкой после изменения; никогда по названию.
- `reconcile(job_id) → Outcome` — проверка подтверждения отклика для `unknown`; отсутствие надписи не доказывает неудачу.

## hh_agent/letters.py (165 строк)

- `PRACTICE_TECHNOLOGIES` — закрытый список из 18 технологий, по которым допускаются «ограниченные утверждения» о практическом знакомстве.
- `LetterGenerator` грузит `profile.json` в неизменяемый `MappingProxyType` (факты нельзя изменить на лету).
- `_language(vacancy)` — язык письма: ≥8 кириллических букв в тексте вакансии → `ru`, иначе `en`.
- `_frame` — фиксированные приветствие/прощание (с URL вакансии).
- `_claims(vacancy, language)` — пары «утверждение → план подготовки» для технологий из списка, упомянутых в вакансии.
- `_fallback` — детерминированное письмо без модели: `python_backend` + до 5 фактов по тегам из текста вакансии, без выдуманных утверждений.
- `generate(vacancy)` — запрос к Chat Completions (`response_format: json_object`): системный промпт со схемой и правилами безопасности, пользовательское сообщение — только данные вакансии (обрезка полей). Ответ жёстко валидируется `_validate`: точный набор ключей, 1–6 уникальных фактов с `python_backend` первым, ≤2 утверждения строго из `allowed_claims`, `preparation` в том же порядке, текст обязан **дословно** совпадать с локальной сборкой `_render`. Любое отклонение → `LetterError` (санитизированная, без секретов).
- Нет ключа/модели → fallback; ошибка HTTP/JSON → понятная ошибка без утечки URL и тела ответа.

## hh_agent/telegram.py (262 строки)

- `HELP`, `STATUS_LABELS` — тексты команд и русские названия статусов.
- `_chunks(text)` — разбивка ≤4000 UTF-16 юнитов без разрыва суррогатных пар.
- `_authorized(sender, chat)` — только личный чат владельца, не бот.
- `_response(text)` — команды-отчёты: `/status`, `/history`, `/clarifications [стр]`, `/letter ID`, `/help`; устаревшие управляющие команды (`/search`, `/pause`, `/resume`, `/reconcile`, `/requeue`, `/queue`, `/settings`) получают пояснение, что Telegram больше не управляет работой.
- `poll_once()` — один getUpdates с long polling (30 с), сортировка по `update_id`, дедупликация через `store.command`; старые callback-кнопки: пустое `answerCallbackQuery` + попытка убрать клавиатуру конкретного старого сообщения (`editMessageReplyMarkup`).
- `flush()` — доставка до одного чанка из outbox за вызов; прогресс чанков персистится; 429 → уважение `retry_after`; пауза ≥1.05 с между сообщениями.
- `_api(method, payload, retry_key, timeout)` — единый клиент; все ошибки санитизируются в `TelegramError` (без токена/URL/тел ответов).

## hh_agent/engine.py (183 строки)

Оркестратор — см. [02-architecture.md](02-architecture.md). Дополнительно:

- `attention(message)` — пауза + `browser_blocked=true` + `attention_reason` + сообщение в outbox; работа с HH останавливается до ручного исправления и перезапуска.
- `notification(job_id, outcome)` — текст уведомления: ссылка, причина, точное письмо, добавленные утверждения и подготовка (только владельцу).
- `digest()` — ежедневная сводка: счётчики статусов, дельта с прошлой сводки, список пропусков из-за тестов.
- `tick()` — приоритеты: блокировка → периодический поиск → отправка → подготовка → дневная сводка.
- `run()` — `start()` + цикл `tick()` каждые 2 с.

## hh_agent/__main__.py (~310 строк)

- `process_lock(state_dir)` — fcntl-блокировка одного экземпляра.
- `initialize` — создание `.env` (0600) из `.env.example` и `.state/` (0700).
- `configure` — интерактивный скрытый ввод ключей; валидация кандидата до записи; ничего не сохраняется при невалидных значениях; предупреждение, если окружение перебивает `.env`.
- `pair` — одноразовый код `/start <code>`, ожидание в личном чате (5 минут), запись `TELEGRAM_OWNER_ID`, проверка отсутствия webhook.
- `api_login` — вход в HH через API: логин + пароль или код SMS/email; капча показывается картинкой (сохраняется в `.state/captcha.png` и открывается через `open`).
- `doctor` — локальные проверки (токен, владелец, ключ+модель, профиль браузера, токен API, Chromium), без сетевых запросов; печатает выбранный источник вакансий.
- `telegram_tasks(bot)` / `run_worker(config, store, source, label)` — общие для обоих источников: три задачи (engine, polling, delivery) через `asyncio.gather`.
- `browser_command` — выбирает источник: есть `.state/api-token.json` → `HHClient` (API, без браузера); нет → Playwright+`Browser` (запасной путь, включая `login` с ручным входом).
- `main` — разбор команд; перехват `ValueError/RuntimeError` (понятное сообщение) и любых прочих исключений (только имя типа — в тексте исключений могут быть учётные данные или текст страниц); `KeyboardInterrupt` → «незавершённая отправка будет проверена при следующем запуске».

## hh_agent/profile.json (25 строк)

Единственный источник фактов профиля: `version`, `sources` (ссылки на спеки), 16 фактов `{id, ru, en, tags}`. Примеры: `python_backend`, `fastapi`, `django_drf`, `postgresql`, `fitstars_ai`, `api_integration`, `nginx` и др.

## Прочее

- `tests/` — 156 тестов, карта покрытия в [09-testing.md](09-testing.md).
- `scripts/setup.command`, `scripts/start.command` — zsh-обёртки меню/запуска (меню включает вход через API и запасной вход через браузер).
- `pyproject.toml` — пакет `hh-personal-agent`, entry point `hh-agent`, зависимости: playwright, httpx, python-dotenv, cryptography; package-data: `profile.json`.
- `requirements.lock` — замороженные версии (playwright 1.60.0, httpx 0.28.1, cryptography 45.0.7 и пр.).
- `.env.example` — образец настроек (полный список переменных в [11-glossary.md](11-glossary.md)).
