# Поведенческий чек-лист job_finder (базовая линия перед этапом 1b)

> **Статус:** этап 0 плана ([12-multiplatform-plan.md](../knowledge-base/12-multiplatform-plan.md)).
> Составлен **по коду и тестам** (не по КБ) 05.11.2026 на ветке `feature/multiplatform`,
> от checkpoint-коммита `0b42719`. Прогон тестов: 156/156, Python 3.12.12.
> Назначение: зафиксировать текущее поведение, чтобы этап 1b (удаление автоотклика)
> мог удалить только отправку, не задев остальное; пункты, которые 1b
> изменяет/удаляет, помечены **[1b]**.

## 1. Конфигурация (`job_finder/config.py`)

- [ ] Загрузка из `root/.env` (dotenv, без интерполяции); переменные окружения имеют приоритет над файлом (`Config.load`, `from_values`).
- [ ] Загрузка конфига **не создаёт** состояние (`/.state`, БД) и не требует секретов — `test_dry_defaults_do_not_create_state_or_require_secrets`.
- [ ] Валидация: `TELEGRAM_BOT_TOKEN` по шаблону `\d+:[A-Za-z0-9_-]+`; числовые значения — положительные целые; булевы — `true/false/1/0`; `LLM_BASE_URL` — HTTPS без пароля/параметров; `HH_RESUME_ID` должен совпадать с согласованным резюме — `test_invalid_values_are_rejected_without_echoing_secrets`.
- [ ] Ошибки валидации не «эхо»-выводят секреты; `repr(Config)` не содержит ключей/токенов — `test_repr_never_contains_credentials`.
- [ ] Поля: `live` (из `HH_LIVE`) **[1b]** — управляет реальной отправкой; `headless` (`HH_HEADLESS`); `search_interval` (по умолч. 21600 с), `send_interval` (90 с) **[1b]**, `max_search_pages` (100).
- [ ] Модель LLM не установлена, пока пользователь не настроит (`test_model_is_unset_until_user_configures_it`).

## 2. Модели (`job_finder/models.py`)

- [ ] `Vacancy`: замороженный dataclass; поля `id, title, description, company, salary, remote, test_required, closed, already_applied, requirements`; `test_required`/`already_applied` — **[1b]** (метка теста вместо отклонения; `already_applied` уходит вместе с откликами).
- [ ] `Vacancy.url`: только для числовых id; иначе `ValueError` («Некорректный ID вакансии»); URL `https://hh.ru/vacancy/<id>`.
- [ ] Сериализация `to_dict`/`from_dict` (salary вложенным объектом).
- [ ] `Salary(minimum, maximum, currency, period, raw)`; `Decision(status, reason)`; `Letter(text, fact_ids, added_claims, preparation)`; `Outcome(status, evidence, reason)`.
- [ ] Исключения: `AttentionRequired` (нужен человек), `VacancyUnavailable` (вакансия недоступна аккаунту).

## 3. Правила отбора (`job_finder/rules.py`)

- [ ] `parse_salary`: чистит NBSP/NNBSP, извлекает числа; валюта по паттернам (RUB/USD/EUR/KZT, иначе `unknown`); период (month/hour/year/day, иначе `unknown`); одно число → «до»/«от»; два числа → вилка; >2 → неоднозначно (`lo=hi=None`); неразобранная строка не «додумывается» — `test_salary_text_is_not_guessed`.
- [ ] `evaluate`:
  - [ ] `closed` → `closed` («Вакансия закрыта»).
  - [ ] `test_required` → `rejected` «Пропущена: предложено пройти тест» **[1b]** (станет меткой в карточке, не отказом).
  - [ ] Python обязателен в названии/описании/требованиях (`\bpython\b|\bпитон\w*`); название компании «Python» не считается релевантностью — `test_company_python_is_not_relevance`.
  - [ ] `remote is False` → `rejected`; `remote is None` → `needs_clarification`.
  - [ ] Командировки: упоминание → `rejected`; «без командировок»/«no business travel» — не считается.
  - [ ] Территориальные ограничения удалёнки («только РФ», «из …») → `needs_clarification`.
  - [ ] Грейд: Middle/Senior/… (RU+EN) распознаётся в названии и контекстах; «под руководством Senior» не повышает грейд — `test_mentoring_by_senior_is_not_the_vacancy_grade`; явный Junior в названии без Middle-маркера → грейд не Middle (кроме явного смешанного «Junior/Middle»); Junior без зарплаты → `rejected`, остальные без зарплаты → `needs_clarification`.
  - [ ] Зарплата: не RUB или не месяц → `needs_clarification`; вилка неоднозначна/невалидна → `needs_clarification`; верх ≥ 150 000 → `ready`; максимум < 150 000 → `rejected`; «от X» ≥ 150 000 → `ready`; «от X» < 150 000 + Middle → `ready`, иначе `needs_clarification` — `test_salary_boundaries_and_grade`.

## 4. Хранилище (`job_finder/store.py`)

- [ ] SQLite, WAL, файл 0600; `PRAGMA user_version` 0–3; более новая версия → ошибка без даунгрейда — `test_future_database_version_is_rejected_without_downgrade`.
- [ ] Таблицы: `jobs`, `attempts` **[1b]**, `settings`, `inbox`, `outbox` (+миграция v2: колонка `kind`, legacy-`success` помечается), `skip_events`.
- [ ] Статусы: `found, rejected, needs_clarification, ready, sending, sent, unknown, error, closed`; `sending/sent/unknown` **[1b]** (активные отправки удаляются; данные — только в 1c).
- [ ] Пилот `137924141` (`sent`, с подтверждением) зашит в схему и никогда не становится в очередь — `test_confirmed_pilot_is_never_queued`.
- [ ] `discover` — идемпотентно (`INSERT OR IGNORE`, статус `found`).
- [ ] `decide` не перезаписывает `sent/sending/unknown`; `make_ready` только из `found`; `save_letter` не трогает `sending/sent/unknown`.
- [ ] `claim` **[1b]** — атомарный переход `ready → sending` с записью в `attempts`, блокируется паузой; два соединения не могут заявить один отклик — `test_two_connections_cannot_claim_same_application`.
- [ ] `finish` **[1b]** — `sent` требует непустого подтверждения; из `sending/unknown` только в `sent/unknown`; иначе «Отправка начата; нужно проверить историю»; атомарно с уведомлением — `test_success_and_notification_are_atomic`.
- [ ] `recover` **[1b]** — `sending → unknown`, `attempts.sending → unknown`, `ready` без письма → `found`; **никогда не возвращает в очередь** прерванную отправку — `test_crash_recovery_never_requeues_send`.
- [ ] Outbox: доставка только `reply`/`success`; `event` сохраняется без проактивной доставки — `test_generic_events_are_retained_without_proactive_delivery`, `test_explicit_notification_kinds_filter_before_delivery_limit`.
- [ ] `skip_events`: пропуск из-за теста фиксируется один раз и появляется в ежедневной сводке — `test_terminal_test_skip_survives_search`.
- [ ] `command` (inbox) — дедуп по `update_id`, сохраняет `telegram_offset` и ответ; `rows` — пагинация по статусам и порядку — `test_rows_pagination_respects_status_and_order`.
- [ ] Настройки: `paused` (по умолчанию `true`), `last_search`, `search_report`, `browser_blocked`, `attention_reason`, `next_send_at` **[1b]**, `digest_day`, `last_digest_at`.

## 5. Движок (`job_finder/engine.py`)

- [ ] `start` (локальный запуск — единственное управление): `recover`, сброс legacy-флагов (`search_requested`, `reconcile_requested`, `browser_blocked`), при `unknown` — пауза + сверка до продолжения; сразу `search` — `test_local_start_searches_immediately_and_clears_legacy_manual_pause`, `test_start_reconciles_interrupted_send_before_continuing`.
- [ ] `attention` — пауза, `browser_blocked`, причина, уведомление в outbox.
- [ ] `search`: вызывает источник, сохраняет новые id как `found`, пишет `last_search` + `search_report`; `AttentionRequired`/ошибки → `attention`; неполнота выдачи — в примечаниях — `test_discovery_includes_all_ids_and_preserves_pilot`.
- [ ] `prepare`: только из `found`; `already_applied` → `sent` «Страница HH подтверждает ранее отправленный отклик» **[1b]**; `ready` → письмо + `make_ready`; иначе `decide` (+ сообщение при `needs_clarification`); `VacancyUnavailable` → `needs_clarification`; ошибка → `error` — `test_preparation_clarification_is_saved_without_notification`, `test_unavailable_vacancy_requires_clarification_and_next_job_is_prepared`.
- [ ] `send` **[1b]**: только при `live`, не на паузе, `ready` с письмом; повторное чтение перед отправкой; изменившиеся условия/зарплата блокируют отправку — `test_changed_salary_blocks_prepared_application`; после claim ошибка → `unknown` (без автоповтора) — `test_unknown_is_paused_and_never_retried`; `next_send_at` = now + `send_interval`.
- [ ] `reconcile` **[1b]**: сверка `unknown` без повторной отправки — `test_reconcile_confirms_without_sending_again`.
- [ ] `digest`: сводка очереди по статусам; delta с `last_digest_at`; список пропусков из-за теста; пилот исключён из сводки.
- [ ] `tick`: блок при `browser_blocked`; периодический поиск по `search_interval`; отправка по `next_send_at`; подготовка `found`; ежедневная сводка — `test_daily_digests_do_not_send_unsolicited_notifications`.
- [ ] В режиме просмотра (`live=False`) отправка не выполняется даже после «resume» — `test_preview_cannot_submit_even_after_resume` **[1b]**.

## 6. Письма (`job_finder/letters.py`)

- [ ] Язык по контенту вакансии: ≥8 кириллических символов → RU, иначе EN — `test_english_vacancy_gets_english_without_language_proficiency_claim`.
- [ ] Без LLM-ключа/модели — fallback: `python_backend` + до 5 связанных фактов по тегам, без выдуманных утверждений — `test_fallback_uses_related_facts_and_no_invented_claims`.
- [ ] Валидация ответа модели fail-closed: точная схема `{text, fact_ids, added_claims, preparation}`, 1–6 фактов с `python_backend` первым, ≤2 утверждений из закрытого allowlist (`PRACTICE_TECHNOLOGIES`), подготовка дословно совпадает; `text` обязан дословно собираться из кадров/фактов/утверждений — `test_model_choices_are_validated_and_request_contains_no_credentials`, `test_unknown_facts_unlogged_claims_and_career_fabrications_fail_closed`.
- [ ] Текст вакансии — данные, не инструкции; произвольный текст вакансии не попадает в письмо как факт карьеры — `test_untrusted_title_cannot_enter_letter_as_career_fact`.
- [ ] Ошибки HTTP/JSON санитизируются (без ключа/URL/тела) — `test_http_errors_do_not_expose_key_url_or_body`, `test_malformed_provider_envelopes_fail_with_sanitized_letter_error`.

## 7. Браузерный адаптер (`job_finder/browser.py`)

- [ ] `safe_hh_url`: только https, домен `hh.ru`/`*.hh.ru`, порт 443/пусто, без userinfo; иначе `AttentionRequired`.
- [ ] `goto`/`check_access`: капча/«слишком много запросов»/ограничение → `AttentionRequired`; URL входа → `AttentionRequired`; `require_auth` — проверка профиля соискателя.
- [ ] `read`: 404/410 → закрытая вакансия (не сбой); архив/удалена → `closed`; активная вакансия без описания → `AttentionRequired`; недоступность конкретной вакансии → `VacancyUnavailable` (локально для аккаунта, не блокирует всю работу) — `test_deleted_vacancy_is_closed_not_auth_failure`, `test_specific_vacancy_access_denial_is_local_to_signed_in_account`, `test_generic_403_still_blocks_browser_work`.
- [ ] Поля: title/company/salary/`work-formats-text` → `remote`, key-skills → `requirements`; `already_applied` по подтверждению на странице — `test_python_in_key_skills_is_included`.
- [ ] Тест: форма отклика + баннеры вакансии (локальные блоки, не вся страница, не реклама скиллов) — `test_visible_test_on_vacancy_prevents_form_and_submit`, `test_hidden_clipped_and_generic_ad_do_not_block`; сканирование переживает замену DOM — `test_test_scan_survives_page_replacing_loading_elements`.
- [ ] `search`: страницы SERP, сбор id, пагинация, дедуп, при усечении выдачи — разделение по диапазонам опыта, `incomplete` + notes — `test_search_uses_all_pages_and_deduplicates`.
- [ ] `apply` **[1b]**: подтверждение «откликнулись/отправлен» → `sent`; тест → пропуск; точное резюме (только согласованный id, не похожий заголовок); форма отклика, письмо, доп. вопросы анкеты → `needs_clarification`; пауза до финального клика → не отправляет — `test_success_submits_once_with_exact_resume_and_letter`, `test_pause_gate_prevents_final_click`, `test_missing_exact_resume_never_uses_similar_title`; после начала отправки любой сбой → `unknown` — `test_unknown_result_has_no_automatic_retry`.
- [ ] `reconcile` **[1b]**: подтверждение на странице → `sent`, иначе `unknown` (отсутствие бейджа не очищает unknown).

## 8. API-адаптер hh (`job_finder/hh.py`)

- [ ] `TokenStore`: `.state/api-token.json`, 0600, атомарная запись (.tmp + `os.replace`) — `test_token_store_is_atomic_and_private`; сломанный файл → `{}`.
- [ ] `HHClient`: rate limit ≥0.4 с; санитизация ошибок (без токенов/тел) — `test_errors_are_sanitized`; заголовки Android-приложения; 403 → refresh один раз, потом `api-login`; капча → `AttentionRequired`.
- [ ] `vacancy_from_json`: id/title/description(strip_html)/company/salary(RUR→RUB, gross)/remote(work_format, fallback schedule)/has_test/relations → `already_applied`/closed/requirements из key_skills — `test_vacancy_conversion`.
- [ ] `search`: пагинация до `MAX_RESULTS`/`max_pages`, дедуп, отчёт о полноте, разделение по опыту при усечении — `test_search_paginates_dedupes_and_reports`, `test_search_splits_experience_when_capped`.
- [ ] `read`: 404/410 → закрытая; `me` — проверка входа.
- [ ] `apply` **[1b]**: повторное чтение, `already_applied` → `sent`; gate (`before_send` = claim в БД) до отправки; `POST /negotiations`; редирект → `needs_clarification`; подтверждение поиском negotiation в списке откликов (2 повтора) — `test_apply_respects_gate_and_sends_negotiation`, `test_apply_redirect_requires_clarification`, `test_apply_limit_error`; после claim транспортная ошибка/капча → `unknown` — `test_apply_transport_error_after_claim_is_unknown`, `test_apply_captcha_after_claim_is_unknown`.
- [ ] `reconcile` **[1b]**: negotiation найден → `sent`, иначе `unknown` — `test_reconcile_confirms_or_stays_unknown`.
- [ ] `authorize` (api-login): по паролю и по OTP (SMS/email), капча при входе, `loginTrustFlags` RSA-OAEP(SHA-256) с `ts` — `test_trust_flags_are_rsa_oaep_with_timestamp`, `test_authorize_by_password_full_flow`, `test_authorize_by_otp_full_flow`, `test_authorize_solves_login_captcha`; неожиданный OAuth-ответ → ошибка; неудачный вход не оставляет файл токена — `test_authorize_failure_does_not_leave_token_file`.

## 9. Telegram (`job_finder/telegram.py`)

- [ ] Только владелец в личном чате с ботом; чужие сообщения не раскрывают данные — `test_only_owner_in_own_private_chat_can_read`, `test_clarifications_never_disclose_to_other_users_or_groups`.
- [ ] Дедуп обновлений по `update_id`/offset; ответы — в `inbox`/`outbox`, не повторяются — `test_read_requests_are_durable_and_duplicate_updates_do_not_repeat_replies`.
- [ ] Команды только чтения: `/status` (режим, пауза, счётчики, последний поиск, политика уведомлений), `/history` (до 20 последних), `/letter ID`, `/clarifications [страница]` (пагинация по 20), `/help`; старые управляющие команды (`/search`, `/pause`, `/resume`, `/reconcile`, `/requeue`, `/settings`, `/queue`) — «retired» и не меняют состояние — `test_retired_pause_and_bad_letter_arguments_are_safe`, `test_all_retired_commands_leave_work_state_and_jobs_unchanged`.
- [ ] Callback-запросы (старая клавиатура): пустой ack, снятие клавиатуры один раз; не редактирует чужие/недоступные сообщения — `test_old_owner_callback_is_inert_acknowledged_and_markup_removed_once`, `test_cleanup_never_edits_outsider_group_missing_or_invalid_message`.
- [ ] Доставка: только `reply`/`success` из outbox; чанки ≤4000 UTF-16 без разрыва кодовых точек; прогресс чанков персистентен; темп ≥1.05 с; 429 → bounded retry и повтор того же чанка — `test_delivery_splits_unicode_persists_chunk_progress_and_rate_limits`, `test_delivery_429_retries_same_chunk_after_delay`.
- [ ] Транспортные ошибки санитизированы, offset не откатывается — `test_transport_errors_are_sanitized_and_leave_offset_untouched`; long poll асинхронный с ограниченным таймаутом — `test_long_poll_is_async_and_uses_bounded_timeout`.
- [ ] `STATUS_LABELS` — **[1b]** обновить (`read`/`delivered`/`skipped` вместо `sending`/`sent`/`unknown`).

## 10. CLI (`job_finder/__main__.py`)

- [ ] Команды: `init` (приватный `.env` из примера, не перезаписывает существующий), `configure` (скрытый ввод, валидация до сохранения, чинит невалидный URL без потери секретов, «пустые» ответы сохраняют кастомные значения) — `test_init_is_private_and_does_not_overwrite_settings`, `test_configure_repairs_invalid_url_without_revealing_or_losing_secrets`.
- [ ] `pair`: одноразовый код `/start <code>` в течение 5 минут, только личный чат, отказ при webhook — сохраняет `TELEGRAM_OWNER_ID`.
- [ ] `doctor`: локальные проверки (токен бота, владелец, ключ/модель LLM, профиль браузера, токен API HH, Chromium), без сетевых проверок.
- [ ] `api-login`: пароль или OTP-код, капча сохраняется в `.state/captcha.png` и открывается; после входа — `GET /me` — `test_launcher_starts_worker_directly_without_menu_or_telegram_command` (частично).
- [ ] `run`: процесс-лок на `.state/agent.lock` (`flock`, второй экземпляр → ошибка) — `test_local_process_lock_prevents_second_worker`; источник — API HH при наличии токена, иначе браузер — `test_run_uses_api_source_when_token_present_without_browser`; Telegram-сбой не останавливает локальную работу — `test_telegram_outage_does_not_stop_local_search_or_preparation`.
- [ ] `search`: разовый поиск без отправок; `status`: пауза и счётчики.
- [ ] Секреты записываются без исполнения/интерполяции значений — `test_secrets_written_without_executing_or_interpolating_values`; необработанные исключения не печатают HTTP/браузерный текст с секретами.

## 11. Что затронет этап 1b (сводка для сверки)

| Область | Сейчас | После 1b |
| --- | --- | --- |
| `browser.py` | чтение + поиск + `apply`/`reconcile` | чтение/поиск удаляются вместе с Playwright-фолбэком по плану; `apply`/`reconcile`/`sent_evidence`/`has_test`(как отказ)/`select_resume` удаляются |
| `hh.py` | `apply`/`reconcile`/`_negotiation_ids`/`_confirm` | удаляются; остаётся поиск/чтение/вход |
| `engine.py` | `send`, `reconcile`, `notification` откликов | удаляются; пауза отправки → **пауза доставки** (DELIVER) |
| `store.py` | `claim`/`finish`/`recover`, статусы `sending`/`sent`/`unknown`, таблица `attempts`, `skip_events` | активный код удаляется; данные/схема — только в 1c |
| `rules.py` | тест → `rejected` | тест → **метка в карточке** (не отказ) |
| `config.py` | `HH_LIVE`, `send_interval`, `resume_id` | удаляются вместе с отправкой |
| `telegram.py` | `STATUS_LABELS` с `sending`/`sent`/`unknown` | `read`/`delivered`/`skipped` |

**Критерий выхода этапа 0:** зелёный прогон тестов (156/156 на 05.11.2026), ветка `feature/multiplatform` создана от checkpoint `0b42719`, чек-лист зафиксирован в этом файле.
