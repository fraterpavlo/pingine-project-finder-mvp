# ProjectFinder — главный файл для агентов

> **Перед началом любой работы прочитай этот файл целиком + последние 30–50 строк [AGENT-LOG.md](AGENT-LOG.md).** Журнал — общая память между агентами: что уже пробовали, какие решения приняли, на какие грабли наступили. После окончания своей работы добавь короткую запись в журнал.

---

## Что это

Автоматический инструмент поиска работы для **одного** разработчика. Запускается на инфраструктуре **Claude Cowork** (cron-задачи) + **локальная Python-машина** (демоны). Цель — без участия человека:

1. Сканировать источники вакансий (Telegram-каналы, web-доски).
2. Оценивать каждую найденную вакансию относительно профиля разработчика.
3. Инициировать первый контакт с работодателем (email или Telegram) от имени конкретного человека.
4. Вести последовательный, контекстный диалог с HR — до приглашения на звонок.
5. В сложных ситуациях (нет данных, нестандартный вопрос, deep-dive вне профиля) — эскалировать оператору через Telegram-бот, который умеет одобрить/отредактировать/отклонить ответ.

**HR не должен догадаться, что общается с инструментом.** Никаких упоминаний автоматизации, «нашей команды», «бота». Один человек, лично откликнувшийся на вакансию.

Резюме разработчика (PDF) лежит в `project-finder/data/resumes/` (на момент написания может отсутствовать — добавляется по мере работы). В будущем — динамическая адаптация резюме под вакансию; сейчас базовая работа важнее.

---

## Архитектура: два слоя

### Слой 1 — Cowork-скиллы (когнитивный)
Запускаются как **scheduled tasks** в Claude Cowork. Делают всю работу с текстом и решениями. **НЕ имеют прямого доступа к БД**.

Три cron-задачи:
1. **`projectfinder-scan-sources`** (cron `0 */4 * * *`) → скилл [scan-sources](project-finder/skills/scan-sources/SKILL.md). Web-источники из `sources.json`. Эмитит intents для вставки jobs(status='new'). Telegram-каналы НЕ обрабатывает (это локальный `telegram_scanner.py`).
2. **`projectfinder-evaluate-and-initiate`** (cron `15 */2 * * *`) → скилл [evaluate-and-initiate](project-finder/skills/evaluate-and-initiate/SKILL.md). Берёт jobs(status='new') → вызывает [evaluate-job](project-finder/skills/evaluate-job/SKILL.md) для оценки → вызывает [generate-draft](project-finder/skills/generate-draft/SKILL.md) для генерации первого сообщения → эмитит intents (создаёт conversation, outgoing, при borderline — нотификацию оператору). В конце шлёт cycle-summary в TG-бот.
3. **`projectfinder-process-dialogues`** (cron `* * * * *`) → скилл [dialogue-agent](project-finder/skills/dialogue-agent/SKILL.md). Берёт incoming(status='new'), классифицирует HIGH/MEDIUM/LOW. HIGH → автоответ. MEDIUM → ответ + ревью оператору. LOW → эскалация без ответа.

### Слой 2 — Локальные Python-демоны (транспорт + БД)
Запускаются командой `py -3 project-finder/scripts/projectfinder.py` на машине оператора. Шесть демонов с auto-restart:

1. **`ops_applier.py`** — единственный «писатель» в БД со стороны Cowork. Читает intent-файлы из `data/intents/pending/`, применяет через `pf_db.*`, раз в 60с публикует `data/snapshot.sqlite` (read-only) для Cowork-чтения.
2. **`telegram_scanner.py`** — Telethon, сканирует TG-каналы из `sources.json`, прямой `pf_db.upsert_job` (можно — он локальный).
3. **`telegram_io.py`** — Telethon listener (входящие DM) + sender (outgoing(status='ready') с two-phase commit и human-like задержкой).
4. **`email_io.py`** — SMTP send + IMAP poll. Читает identity для `From:` из профиля разработчика (`developers/<id>.json` → `fixed.email_identity`).
5. **`telegram_notifier.py`** — шлёт notifications(pending) в Telegram-бот оператора. Inline-кнопки появляются, если в notification есть `outgoing_id`.
6. **`bot_handler.py`** — обрабатывает callback-кнопки (✅/✏️/❌) и команды `/review`, `/status`, `/start`.

---

## Жёсткие правила (нарушать НЕЛЬЗЯ)

### Доступ к БД из Cowork

> **`project-finder/data/projectfinder.sqlite` — НИКОГДА не открывать из Cowork-скилла.** Ни на чтение, ни на запись. Не копировать в sandbox.
> **Чтение** — только `data/snapshot.sqlite` (read-only, lag до 60 с).
> **Запись** — только через `pf_intents.emit(operation, params)` или `pf_intents.emit_batch([...])`.

Причина: Cowork-sandbox монтирует папку через FUSE. SQLite в WAL не работает на FUSE (`disk I/O error`). Прежний подход «копировать в sandbox → править → копировать обратно» необратимо портил индексы и стирал записи демонов. История инцидента — в [CONSOLIDATED-REVIEW.md §2](CONSOLIDATED-REVIEW.md).

Локальные демоны (`telegram_scanner.py`, `email_io.py`, `telegram_io.py`) пишут в БД напрямую — это нормально, они на той же машине, что и БД, без FUSE.

### Идемпотентность и атомарность

- Связку «conversation + outgoing + conversation_message» эмитить ОДНИМ `emit_batch` — `ops_applier` обернёт в `BEGIN IMMEDIATE` / `COMMIT`. Без этого можно получить outgoing без записи в conversation_messages → dialogue-agent не увидит истории.
- UNIQUE-индексы на `jobs.url`, `incoming_messages.imap_message_id` дополнительно защищают от дублей.
- Two-phase commit для outgoing: `ready → sending → sent/failed`. `recover_stuck_sending(600)` восстанавливает зависшие.

### Маскировка под человека

- Никаких упоминаний бота, автоматизации, агентства, «нашей команды», «we».
- Только первое лицо единственного числа.
- Личные факты — только из `developers/<id>.json`. Никаких выдуманных компаний, цифр, дат.
- Если для уверенного ответа нужно что-то выдумать → классификация LOW → эскалация в TG-бот.
- Identity в `From:` email — из `developers/<id>.json → fixed.email_identity`. Не из `email-config.json` (там только транспорт).

### Эскалация при сомнениях

- LOW в dialogue-agent → `insert_escalation` + `insert_notification(urgency='high')` БЕЗ outgoing.
- MEDIUM в dialogue-agent → outgoing(status='needs_review') + notification с inline-кнопками.
- C-grade или borderline в evaluate-and-initiate → outgoing(status='needs_review') + notification.
- Системные проблемы (snapshot пропал, конфиг не загрузился) → `pf_intents.emit("notify_admin", {...})`. Helper сам подставит chat_id из `notifications-config.json` (получатель с `is_admin=true`).

### Принципы написания SKILL.md (ОБЯЗАТЕЛЬНО для агентов)

Скилл целиком грузится в контекст Cowork-агента ДО старта работы. Каждый лишний абзац — это съеденный токен, который мог бы пойти на анализ snapshot, истории диалога или профиля. Цель — **минимум, чтобы агент мог корректно выполнить задачу**.

**Что ДОЛЖНО быть в SKILL.md:**
1. Frontmatter (`name` + `description`) — кратко, одной строкой.
2. Контракт I/O — что на входе, что на выходе. Один блок, без размазывания.
3. Алгоритм по шагам — короткими предложениями, в порядке выполнения.
4. Минимум кода — только если без примера непонятно (1 пример на шаг, без длинных комментариев).
5. Жёсткие правила, специфичные для ЭТОГО скилла, — буллетами.

**Чего НЕ должно быть в SKILL.md:**
- Дублирование CLAUDE.md (FUSE/snapshot/intents — это уже в CLAUDE.md, агент его прочитал; в скилле — только ссылка).
- Длинные объяснения «почему так» — историю и обоснования смотри в `AGENT-LOG.md` / `CONSOLIDATED-REVIEW.md`.
- Длинные примеры классификации (HIGH/MEDIUM/LOW и т.п.) — 2–3 примера хватит.
- Куски кода с подробными комментариями вместо лаконичных правил.
- Повтор frontmatter в теле документа.
- «Ключевые принципы» в конце, если они уже сказаны в алгоритме.

**Целевой размер:**
- Простой скилл (одно действие, один контракт): **до 100 строк / ~5 КБ**.
- Сложный скилл с маршрутизацией и батчами: **до 250 строк / ~12 КБ**.
- Если получается больше — сначала попробуй вынести что-то в CLAUDE.md или сократить, и только если действительно невозможно — оставляй большим, но в AGENT-LOG объясни, почему.

**Перед сохранением SKILL.md спроси себя:**
- Это правило/факт уже есть в CLAUDE.md? Если да — удали из скилла, оставь ссылку.
- Этот пример нужен агенту, чтобы выполнить задачу, или это страховка для меня-автора? Если второе — удали.
- Если убрать этот абзац — скилл сломается? Если нет — удали.

### Принципы записи в AGENT-LOG.md (ОБЯЗАТЕЛЬНО)

AGENT-LOG.md — это **не дневник твоей работы и не отчёт перед оператором**. Это рабочая память для будущих агентов. Цель — чтобы агент через месяц прочитал последние 30–50 строк и сразу понял: что уже пробовали, какие архитектурные решения приняли и почему, на какие грабли наступили, какие контракты можно легко нарушить.

**Перед записью спроси себя: пригодится ли это будущему агенту, который не помнит ничего из этого разговора?** Если ответ «нет» — не пиши.

**Что ПИСАТЬ:**
- **Архитектурные решения**, которые НЕ очевидны из кода. Пример: «Выбран intent-queue (Variant A), а не journal_mode=DELETE — потому что copy-back через FUSE необратимо портит индексы».
- **Грабли**, на которые наступили: симптом + причина + как обойти. Если это устойчивая грабля будущего — добавь в секцию «Известные грабли», не в журнал.
- **Контракты, которые легко нарушить**: «Поле называется `score_letter`, не `score` — рассогласование молча ломает evaluate-and-initiate». Если это устойчивый инвариант — в «Известные грабли».
- **Решения, отвергнутые с обоснованием**: «Не пошли по пути SSE, потому что Cowork sandbox блокирует long-poll». Иначе следующий агент попробует то же самое.
- **Открытые вопросы** — то, что НЕ закрыто и требует чьего-то действия (часть «Открытые вопросы»).

**Что НЕ ПИСАТЬ:**
- **Рутинные операции:** «обновил .gitignore», «переименовал файл», «сделал commit», «сгенерировал PDF». Текущее состояние видно в репо/БД одной командой.
- **Хронологию «что я сделал»**: для этого есть `git log`. Не дублируй.
- **Промежуточные состояния и метания**: «попробовал X, не получилось, попробовал Y, тоже не получилось, в итоге сделал Z». Пиши только финальный вывод и **почему** Z — иначе журнал утонет в шуме.
- **Перечисление файлов, которые правил**: это в `git diff` коммита.
- **Артефакты твоей работы**: «создал такой-то скилл», «записал секцию X в CLAUDE.md» — следующий агент это и так увидит.
- **Операционную активность с Cowork**: «синхронизировал три задачи», «перепрогнал». Текущее состояние Cowork-задач не хранится в репо — журнал об этом ничего не скажет.

**Формат:** одна запись = 3–8 строк максимум. Если получается больше — значит, ты пишешь отчёт оператору, а не заметку для будущего агента. Перенеси в CONSOLIDATED-REVIEW.md.

**Перед сохранением записи задай себе три вопроса:**
1. Через месяц следующий агент прочитает это и узнает что-то, чего нет в коде/конфигах/git-истории/CLAUDE.md?
2. Если бы я НЕ написал эту запись, что-то реально потерялось бы?
3. Эта информация защитит от ошибки, которую кто-то ещё может повторить?

Если на все три «нет» — не записывай.

### Cascade-проверка при любом изменении (ОБЯЗАТЕЛЬНО)

Любая правка в архитектуре, контракте между скиллами, схеме БД, формате конфига, имени поля, цепочке вызовов — почти всегда требует синхронных правок в **нескольких** местах. Точечное изменение, которое забыло про каскад, ломает целостность инструмента молча и обнаруживается только в проде. Это **главная причина регрессий** в этом проекте.

**Перед завершением работы агент ОБЯЗАН пройти этот чек-лист:**

1. **Затронутая зона.** Какой контракт/файл/структуру я изменил? Например: «переименовал поле `score` → `score_letter` в evaluate-job/SKILL.md».
2. **Поиск всех пользователей.** `Grep` по имени поля / функции / пути / константы по всему проекту. Кто читает или пишет эту сущность? Найди ВСЁ:
   - другие SKILL.md;
   - Python-скрипты в `scripts/`;
   - конфиги в `config/`;
   - документация: `CLAUDE.md`, `AGENT-LOG.md`, `CONSOLIDATED-REVIEW.md`;
   - smoke-тесты и примеры в этих файлах.
3. **Синхронизация.** Каждое найденное использование — либо адаптировать под новый формат, либо явно решить «back-compat: оставляем как есть» (и записать причину в AGENT-LOG.md).
4. **Smoke-проверка.** Если правка влияет на критичный путь (БД, identity, маршрутизация) — запусти smoke-тест из конца этого файла. Должен пройти.
5. **Запись в AGENT-LOG.md.** В журнале — короткая заметка: что изменил, какие файлы пришлось подтянуть, какие осознанно оставил.

**Примеры реальных каскадов (из истории проекта):**

- Поменять имя поля `score` → `score_letter` в `evaluate-job/SKILL.md` → нужно обновить также `evaluate-and-initiate/SKILL.md` (он читает результат), `pf_policy.py` (если ссылается), все примеры в CLAUDE.md и AGENT-LOG.md.
- Поменять `from_address` в профиле разработчика → нужно проверить `links.email` (тот же профиль, разные поля), все примеры/asserts в `CLAUDE.md`, `AGENT-LOG.md`, `CONSOLIDATED-REVIEW.md` (smoke-проверки), убедиться что `email-config.json → smtp.username` совпадает (иначе Gmail перепишет From).
- Перейти с прямого `pf_db` на `pf_intents` в Cowork-скилле → проверить ВСЕ скиллы, не остался ли где-то старый `import pf_db`. Один забытый — и FUSE снова портит БД.
- Добавить новую операцию в `pf_db` → добавить её в `pf_intents.ALLOWED_OPERATIONS`, в `ops_applier.DISPATCH`, упомянуть в SKILL.md тех скиллов, которым она нужна.
- Удалить устаревший SKILL.md (`notify-human`, `tg-outreach`) → найти все ссылки на этот скилл в других SKILL.md и в коде; либо переадресовать, либо удалить вызов.

**Если не уверен, что нашёл всё** — лучше сделать `Agent` (Explore) с задачей «найди все места, где упоминается X», чем оставить рассинхрон.

---

## Где что лежит

```
ProjectFinder/                            ← корень репо
├── CLAUDE.md                             ← этот файл
├── AGENT-LOG.md                          ← общий журнал агентов (читать перед работой!)
├── CONSOLIDATED-REVIEW.md                ← полная история ревью + каталог решённых проблем
├── REVIEW-2026-04-15.md                  ← исторический снэпшот (только для сверки)
└── project-finder/
    ├── config/                           ← НАСТРОЙКИ (JSON + Markdown)
    │   ├── secrets.json                  ← git-ignored: SMTP, bot_token, Telethon api_id/api_hash
    │   ├── secrets.example.json          ← шаблон
    │   ├── sources.json                  ← список источников вакансий
    │   ├── positions.json                ← позиции, ключевые слова, application_rules
    │   ├── developers/                   ← профили разработчиков (один на бойца)
    │   │   └── test-fullstack.json       ← пока единственный (тестовый интеграционный)
    │   ├── auto-reply-config.json        ← глобальные политики; источник правды — pf_policy.py
    │   ├── email-config.json             ← SMTP/IMAP транспорт. Identity From: НЕ ЗДЕСЬ
    │   ├── notifications-config.json     ← recipients с полем is_admin
    │   ├── telegram-client-config.json   ← api_id/api_hash + session_name
    │   ├── scoring-rules.md              ← правила оценки (читает evaluate-job)
    │   ├── writing-style.md              ← правила тона (читают все генерирующие скиллы)
    │   └── templates/
    │       ├── cover-letter-en.md
    │       └── cover-letter-ru.md
    ├── data/                             ← runtime (всё в .gitignore)
    │   ├── projectfinder.sqlite          ← основная БД (WAL). Демоны пишут напрямую.
    │   ├── snapshot.sqlite               ← read-only копия для Cowork (раз в 60с)
    │   ├── intents/
    │   │   ├── pending/                  ← Cowork эмитит сюда; ops_applier подхватывает
    │   │   ├── applied/                  ← успешно применённые
    │   │   └── failed/                   ← ошибки применения
    │   ├── backups/                      ← (P2-5 ещё не реализован)
    │   └── resumes/                      ← PDF резюме разработчиков
    ├── logs/
    │   ├── projectfinder.log             ← общий лог всех демонов
    │   └── alerts.log                    ← fallback для notify_admin при недоступности TG
    ├── scripts/                          ← Python-демоны и хелперы
    │   ├── projectfinder.py              ← launcher; запускает 6 демонов
    │   ├── pf_db.py                      ← data layer (схема + все функции)
    │   ├── pf_intents.py                 ← Cowork-side helper (emit/emit_batch)
    │   ├── pf_policy.py                  ← decide_outgoing_status (единый источник правды)
    │   ├── pf_secrets.py                 ← deep-merge config + secrets
    │   ├── ops_applier.py
    │   ├── telegram_scanner.py
    │   ├── telegram_io.py
    │   ├── email_io.py
    │   ├── telegram_notifier.py
    │   ├── bot_handler.py
    │   └── reset_db.py                   ← danger: пересоздаёт БД с нуля
    └── skills/                           ← инструкции для Cowork-агентов
        ├── scan-sources/SKILL.md
        ├── evaluate-and-initiate/SKILL.md
        ├── evaluate-job/SKILL.md
        ├── generate-draft/SKILL.md
        └── dialogue-agent/SKILL.md
```

---

## Контракты между скиллами

```
sources.json + positions.json
        ↓
[scan-sources]            эмитит upsert_job(status='new')
        ↓
       [ops_applier применяет → snapshot обновляется]
        ↓
jobs(status='new') в snapshot
        ↓
[evaluate-and-initiate]  ← вызывает [evaluate-job] (оценка)
                         ← вызывает [generate-draft] (текст)
                         эмитит batch(set_job_status, create_conversation,
                                       insert_outgoing, append_conv_msg,
                                       [insert_notification если review])
        ↓
       [ops_applier применяет → snapshot обновляется]
        ↓
outgoing(status='ready') ─→ [email_io / telegram_io] ─→ HR получает письмо
                                                             ↓
                                                        HR отвечает
                                                             ↓
[email_io.check_inbox / telegram_io.on_dm] → pf_db.insert_incoming
                                              (с conversation_id привязкой)
        ↓
incoming(status='new') в snapshot
        ↓
[dialogue-agent]         использует incoming.conversation_id напрямую
                         ← классифицирует HIGH/MEDIUM/LOW
                         эмитит batch(append_conv_msg, insert_outgoing,
                                       append_conv_msg, mark_processed,
                                       [insert_notification для MEDIUM])
                         или эмитит escalation для LOW
        ↓
… цикл повторяется до приглашения на звонок (LOW-ветка) …
```

### Контракт `evaluate-job` → `evaluate-and-initiate`

Поле выхода — `score_letter` (НЕ `score`). См. [evaluate-job/SKILL.md](project-finder/skills/evaluate-job/SKILL.md).

### Контракт `generate-draft` → `evaluate-and-initiate`

Чистая функция. Вход — `{job, developer, channel, previous_first_message_body}`. Выход — `{subject, body, confidence, facts_used, placeholders_left, personalization_facts}`. Никаких файлов, никакого Gmail, ничего в БД. См. [generate-draft/SKILL.md](project-finder/skills/generate-draft/SKILL.md).

### Контракт `dialogue-agent`: conversation_id

Используется напрямую `incoming.conversation_id` (демон уже привязал). НЕ искать через snapshot — read-lag вызовет создание дубля conversation. Поиск через snapshot — только fallback для «сирот» (incoming без conversation_id).

---

## Допустимые операции в `pf_intents`

См. `pf_intents.ALLOWED_OPERATIONS`:
- jobs: `upsert_job`, `set_job_status`
- conversations: `create_conversation`, `set_conversation_status`, `touch_conversation`, `update_conversation_meta`, `append_conversation_message`
- incoming: `insert_incoming`, `mark_incoming_processed` (редко из Cowork)
- outgoing: `insert_outgoing`, `approve_outgoing`, `reject_outgoing`, `update_outgoing_body`
- notifications: `insert_notification`, `ack_notification`, `notify_admin` (последний сам подставляет chat_id)
- escalations: `insert_escalation`, `resolve_escalation`
- service_state: `state_set`
- composite: `batch` (атомарно несколько операций в одной транзакции)

---

## Где жить идентичности и секретам

- **`config/secrets.json`** — git-ignored. SMTP-app-password, bot_token, Telethon api_id/api_hash. Шаблон — `secrets.example.json`.
- **`scripts/projectfinder.session`** — Telethon session. При первом запуске телетон спросит номер + код.
- **`developers/<id>.json` → `fixed.email_identity.from_name` / `from_address`** — From: для SMTP. Если поле отсутствует → fallback на `email-config.json → smtp.username`.
- **`notifications-config.json` → `recipients[*].is_admin = true`** — этот получатель получает системные алерты от `notify_admin`.

---

## Перед стартом нового агента

1. Прочитай этот файл целиком.
2. Прочитай последние 30–50 строк [AGENT-LOG.md](AGENT-LOG.md). Особое внимание — секции «Открытые вопросы» и «Известные грабли».
3. Если задача связана с конкретной проблемой → найди её в [CONSOLIDATED-REVIEW.md](CONSOLIDATED-REVIEW.md) (поиском по ID `P0-NEW-N` / `P1-NEW-N`).
4. **Никогда** не открывай `data/projectfinder.sqlite` напрямую из Cowork.
5. **Перед** правкой любого SKILL.md убедись, что не противоречит контрактам выше.
6. **Во время** правки архитектуры/контракта — пройди cascade-чек-лист (см. блок «Cascade-проверка» выше). Точечная правка без каскада ломает целостность.
7. **После** завершения работы — добавь запись в [AGENT-LOG.md](AGENT-LOG.md) (формат — внутри файла).

---

## Минимальный smoke-тест (запускается локально)

```bash
cd D:/JOB/IVAN.NOVIKOV/ProjectFinder
py -3 -c "
import sys; sys.path.insert(0, 'project-finder/scripts')
import pf_db, pf_policy, pf_intents, ops_applier, email_io
pf_db.init_db()
hc = pf_db.health_check()
assert hc['ok'], hc
assert pf_policy.decide_outgoing_status(score_letter='A', borderline=False) == 'ready'
key = pf_intents.emit('state_set', {'key': 'smoke', 'value': {'ok': True}})
ops_applier.run_once()
assert pf_db.state_get('smoke') == {'ok': True}
ident = email_io.resolve_from_identity({'username': 'fb@x'}, 'test-fullstack')
assert ident == ('Иван Соколов', 'suprrama@gmail.com'), ident
print('SMOKE OK')
"
```

Если smoke падает — **не запускай боевой launcher**. Сначала пойми и опиши проблему в AGENT-LOG.md.
