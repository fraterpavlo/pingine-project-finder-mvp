---
name: generate-draft
description: "Чистая функция: на вход — оценённая вакансия (A/B/C) и профиль разработчика, на выход — словарь с готовым текстом первого сообщения (subject + body) для подстановки в outgoing_messages.body. Никаких файлов, Gmail-черновиков, записей в БД. Скилл вызывается из evaluate-and-initiate, который сам пишет результат через intents."
---

# Generate Draft — генератор первого сообщения работодателю

Чистая функция: вход — словарь, выход — словарь. Никаких побочек: ни файлов в `data/drafts/`, ни Gmail-черновиков через MCP, ни записей в БД. Раньше скилл писал `.md` и открывал Gmail в браузере — это было до перехода на SQLite + intents и SMTP-отправку через `email_io.py`. После архитектурного фикса вся персистентность — задача `evaluate-and-initiate`, который вызывает этот скилл и сам кладёт результат в `outgoing_messages.body` через `pf_intents.emit("insert_outgoing", ...)`.

## Разрешение путей

Найди каталог, содержащий `project-finder/`. Все пути ниже относительны от него.

## Конфиг-файлы (читаются один раз)

1. `project-finder/config/writing-style.md` — **обязательно** правила тона и стиля.
2. `project-finder/config/templates/cover-letter-en.md` — английский шаблон-структура.
3. `project-finder/config/templates/cover-letter-ru.md` — русский шаблон-структура.
4. `project-finder/config/positions.json` — для `application_rules.forbidden_in_communications`.

## Вход (СТРОГИЙ контракт)

```json
{
  "job": {
    "id": "tg-...-123",
    "title": "Senior React Developer",
    "description": "<полный текст вакансии>",
    "company": "Example Corp",            // может быть null
    "language": "ru",                      // "ru" | "en"
    "matched_position": "react-frontend",
    "score_letter": "A",                   // "A"|"B"|"C"
    "yellow_flags": []
  },
  "developer": { /* содержимое developers/<id>.json */ },
  "channel": "email",                      // "email" | "telegram"
  "previous_first_message_body": "..."     // тело прошлого первого письма
                                            // от того же developer_id (для anti-template).
                                            // null если предыдущих нет.
}
```

## Выход (СТРОГИЙ контракт)

```json
{
  "subject": "Отклик на Senior React — Иван Соколов",   // null для channel='telegram'
  "body": "<полный текст сообщения, готовый к отправке>",
  "confidence": "HIGH",                    // "HIGH"|"MEDIUM"|"LOW"
  "facts_used": [                          // что взято из профиля — для аудита
    "developer.fixed.first_name",
    "developer.fixed.total_experience_years",
    "developer.flexible.primary_stack['React']",
    "job.description: 'real-time dashboard'"
  ],
  "placeholders_left": [],                 // массив незаполненных {{name}}-плейсхолдеров;
                                            // если непустой — confidence ОБЯЗАН быть LOW
  "personalization_facts": [               // конкретные факты ИЗ ВАКАНСИИ, использованные в первом абзаце
    "real-time data visualization",
    "Stripe integration"
  ]
}
```

## Алгоритм

### Шаг 1: Выбор шаблона

- `job.language == "ru"` И `channel == "email"` → шаблон-структура `cover-letter-ru.md`.
- `job.language == "en"` И `channel == "email"` → `cover-letter-en.md`.
- `channel == "telegram"` → шаблон НЕ из файла, формат ниже («TG-формат»).

### Шаг 2: Анализ вакансии (для персонализации)

Прочитай `job.description` и выпиши в локальный список 2–3 КОНКРЕТНЫХ факта, на которые сошлёшься в первом абзаце. Примеры:
- упомянутый продукт («B2B-аналитика», «real-time dashboard»);
- конкретная технология, выделенная в вакансии («WebSocket», «GraphQL Federation»);
- интеграция («Stripe», «Auth0», «Google Ads API»);
- доменная специфика («fintech», «healthtech», «logistics»);
- размер аудитории, нагрузки («20k активных соединений», «1M MAU»).

Эти факты пойдут в поле `personalization_facts` выхода и обязательно появятся в `body`. Если ничего конкретного в вакансии нет — `personalization_facts` пустой, и тогда `confidence` понизится (см. Шаг 5).

### Шаг 3: Сборка тела

#### Email-формат (60–150 слов для тела)

Структура (НЕ заполнять как fill-in-the-blanks — переписывай естественно):

```
{{greeting}},

{{одна_строка_про_конкретику_из_вакансии — реальный факт из job.description, не «интересная роль»}}

Я {{role}} с опытом {{years}}+ лет — основной стек {{2-3_релевантные_технологии_из_developer.flexible.primary_stack_пересекающиеся_с_вакансией}}. {{одно_предложение_про_релевантный_проект — из developer.key_projects, без выдуманных компаний}}.

{{естественный_призыв_к_следующему_шагу — «готов обсудить детали», «когда удобно созвониться»}}.

{{sign_off}},
{{first_name}}
```

Конкретные правила:
- `{{greeting}}` — `developer.communication_style.greetings_ru_first_contact[0]` для ru, `_en_first_contact[0]` для en.
- `{{role}}` — выводи из `matched_position`: `react-frontend` → «фронтенд-разработчик» / «frontend developer», `java-backend` → «бэкенд-инженер на Java», `fullstack` → «fullstack-разработчик».
- `{{years}}` — `developer.fixed.total_experience_years`.
- `{{релевантные_технологии}}` — пересечение `developer.flexible.primary_stack` с вакансией. Никогда не выписывай ВСЕ — выбери 2-3 самых релевантных.
- `{{релевантный_проект}}` — выбери из `developer.key_projects` тот, чей `stack` пересекается с вакансией. Используй `description` или один из `highlights`. **Конкретные названия компаний — НЕЛЬЗЯ** (в профиле они как `company_placeholder`). Используй обобщённые описания типа «продуктовая SaaS-платформа», «outsource-проект для немецкого ритейлера».
- `{{sign_off}}` — `developer.communication_style.sign_offs_ru[0]` для ru, `_en[0]` для en.
- `{{first_name}}` — `developer.fixed.first_name`.

**Subject** для email: `Отклик на {{job.title}} — {{first_name}} {{last_name}}` (ru) / `Application for {{job.title}} — {{first_name}} {{last_name}}` (en).

#### TG-формат (50–80 слов)

Telegram-сообщения короче и без формальностей. Шаблон:

```
{{greeting}}! Я {{first_name}}, {{role}} с опытом {{years}}+ лет.

{{одна_строка_про_конкретику_из_вакансии}} — у меня релевантный опыт по {{2_технологии}}.

{{призыв_к_следующему_шагу}}?
```

- `{{greeting}}` — «Привет» (ru) / «Hi» (en); НЕ «Здравствуйте» — TG неформален по умолчанию.
- Без подписи (TG показывает имя из аккаунта).
- Никаких subject.

### Шаг 4: Анти-шаблон (P1-10 fix)

Если получено `previous_first_message_body` — сравни первые 200 символов нового `body` с первыми 200 символами предыдущего. Если совпадают слово-в-слово — переписывай первый абзац, обязательно вставляя ОДИН факт из `personalization_facts`, которого не было в предыдущем письме. Цель: ни два первых сообщения подряд от одного `developer_id` не должны начинаться одинаково.

Альтернативная защита (если `personalization_facts` пуст): начни с конкретного упоминания заголовка — `«Увидел вашу вакансию {{exact_title}}…»` (а не общего «увидел вакансию на React»).

### Шаг 5: Confidence

Назначь `confidence` так:

- **HIGH** — `score_letter == "A"`, нет yellow_flags, есть хотя бы 1 факт в `personalization_facts`.
- **MEDIUM** — `score_letter == "A"` с yellow_flags ИЛИ `score_letter == "B"`.
- **LOW** — `score_letter == "C"` ИЛИ `personalization_facts` пуст ИЛИ `placeholders_left` непуст ИЛИ при генерации пришлось ссылаться на факт, которого нет в `developer.fixed`/`flexible`/`key_projects`.

evaluate-and-initiate использует это значение для маршрутизации (LOW всегда уходит в `needs_review` независимо от политики).

### Шаг 6: Финальная самопроверка перед возвратом

Пройди по чек-листу. Если хоть один пункт провален — переписывай:

- [ ] Каждый личный факт (имя, годы опыта, город, языки, ставка) взят из `developer.fixed`.
- [ ] Каждая технология есть в `developer.flexible.primary_stack` / `strong_secondary` / `tools_familiar` / `can_adapt_quickly`.
- [ ] Ни одна технология из `developer.flexible.NEVER_claim` не упомянута как «опыт».
- [ ] Никаких выдуманных названий компаний (используй обобщённые: «продуктовая компания», «SaaS-платформа», «outsource»).
- [ ] Никакого коллективного языка («мы», «наша команда», «we», «our team»).
- [ ] Никаких упоминаний автоматизации / бота / агентства.
- [ ] Длина: для email body 60–150 слов, для TG 50–80 слов.
- [ ] Стилевая проверка с `writing-style.md` — никаких клише («горю желанием», «команда мечты», «passionate about»).
- [ ] Эмодзи отсутствуют (для первого контакта emoji_policy строго «без»).
- [ ] Если ru — никаких «Best regards», «Kind regards» в подписи.
- [ ] subject (для email) — есть, для TG — null.

## Принципы

1. **Чистая функция.** Никаких файлов, Gmail-черновиков, БД. Только вход → выход.
2. **Конкретное лучше общего.** «Ваш фокус на real-time data visualization» лучше «Ваши интересные технические задачи».
3. **Никогда не выдумывай личных деталей.** Используй placeholder-ы и понизь до LOW, если факта нет.
4. **Маскируйся под индивидуального кандидата.** Первое лицо единственного числа. Без «мы».
5. **Анти-шаблон обязателен.** Если предыдущее первое сообщение от того же developer_id начиналось так же — переписывай первый абзац.
