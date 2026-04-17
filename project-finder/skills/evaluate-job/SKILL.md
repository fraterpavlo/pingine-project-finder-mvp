---
name: evaluate-job
description: "Оценивает одну вакансию по positions.json и scoring-rules.md — присваивает оценку (A/B/C/Skip), извлекает контакт, определяет язык. Используй для любой вакансии, которую нужно оценить, независимо от источника (веб-скан, Telegram, ручной inbox). Поддерживает объявления на русском и английском. Возвращает чистый JSON-словарь — никаких побочных записей в БД, файлов, Gmail-черновиков."
---

# Evaluate Job — скилл оценки одной вакансии

Чистая функция: вход — словарь с описанием вакансии, выход — словарь с оценкой. Никаких сайд-эффектов, БД, файлов, Gmail. Скилл вызывается из `evaluate-and-initiate`, который сам пишет результат в БД через intents.

## Разрешение путей

Определи каталог, содержащий `project-finder/`. Все пути ниже относительны от него.

## Конфиг-файлы

Прочитай эти файлы СНАЧАЛА (один раз на прогон):

1. `project-finder/config/positions.json` — целевые позиции, ключевые слова, rate range, правила отклика.
2. `project-finder/config/scoring-rules.md` — критерии оценки, шкала, red flags.

## Вход

Объект вакансии минимум с полями:
- `title`, `url`, `description` (или `raw_description`), `source_id`, `discovered_at`.
- Опционально: `company`, `contact` (объект `{telegram, email, type}` от telegram_scanner), `language`.

Используй `description` (или `raw_description`) как основной текст. Если описание пустое или очень короткое — фолбэк на `title`. Иногда всё, что есть — заголовок поста.

## Выход (СТРОГИЙ контракт)

```json
{
  "matched_position": "react-frontend",   // id из positions.json или null
  "score_letter": "A",                     // ТОЧНО одно из "A","B","C","Skip"
  "score_value": 11,                       // целое число баллов, 0-15
  "breakdown": [                           // как считал, для аудита
    {"item": "must_match 'frontend' in title", "delta": 2},
    {"item": "must_match 'react' in description", "delta": 2},
    {"item": "nice_to_have 'TypeScript'", "delta": 1},
    {"item": "experience aligned (3+ years)", "delta": 1},
    {"item": "salary $4000 >= $2000", "delta": 2},
    {"item": "clear responsibilities", "delta": 1}
  ],
  "rationale": "React-фокус совпадает с primary_stack, зарплата в коридоре, обязанности чёткие.",
  "red_flags": [],                         // массив строк-меток
  "yellow_flags": [],                      // массив; разрешённые значения см. ниже
  "remote_status": "confirmed",            // "confirmed" | "unclear" | "denied"
  "language": "ru",                        // "ru" | "en"
  "skip_reason": null                      // если score_letter='Skip' — причина одной строкой
}
```

**Поле НАЗЫВАЕТСЯ `score_letter`, не `score`.** evaluate-and-initiate ожидает именно `score_letter` — рассогласование между скиллами это основной источник тихих багов.

## Алгоритм

### Фаза 1 — Обязательные фильтры (Pass/Fail)

#### 1.1 Совпадение позиции

Сравни вакансию с КАЖДОЙ позицией в `positions.json`:
- Совпадение по title, описанию роли, обязанностям — НЕ по точному стеку.
- Используй `role_indicators` для матчинга.
- «Frontend Engineer» подходит под «React Frontend Developer», даже если стек Vue.
- «Full Stack Developer» может подойти под frontend ИЛИ backend в зависимости от уклона обязанностей.
- Если ни одна позиция не подходит → `score_letter='Skip'`, `skip_reason='no position match'`.

#### 1.2 Проверка remote

- Должно допускать remote или гибрид.
- Ищи: `remote`, `hybrid`, `work from home`, `distributed`, `удалённо`, `гибрид`, `из любой точки`.
- Явно on-site без remote → `score_letter='Skip'`, `skip_reason='on-site only'`.
- Если remote-статус неясен (ни подтверждён, ни опровергнут) → НЕ пропускай. Помечай `remote_status: "unclear"` — пусть человек ревьюит.

#### 1.3 Контекстная проверка стоп-слов

Проверь `exclude_keywords` из `global_filters` в positions.json. КОНТЕКСТНО: слово может присутствовать, но не описывать суть вакансии.

PASS-примеры:
- «We are not looking for interns» → НЕ стажировка.
- «Unlike unpaid internships, we offer competitive salaries» → платная.
- «Мы не берём стажёров» → НЕ стажировка.

SKIP-примеры:
- «Intern/Junior Developer wanted» → стажировка → `score_letter='Skip'`, `skip_reason='intern/volunteer'`.
- «Стажёр-разработчик» → стажировка.

### Фаза 2 — Минимальная осмысленность описания (НОВЫЙ фильтр)

ДО подсчёта баллов проверь описание на «пост из одних хэштегов»:

```python
import re

text = (description or "") if isinstance(description, str) else str(description or "")
tokens = text.split()
clean_tokens = [
    t for t in tokens
    if not t.startswith("#")
       and t.strip()
       and not re.match(r"^[\W_]+$", t)   # не «—», «🚀», ":", "—"
]

if not tokens:
    # пустое описание — может быть title-only пост
    if not (title or "").strip():
        return {"score_letter": "Skip", "skip_reason": "empty_post", ...}

if tokens and (sum(1 for t in tokens if t.startswith("#")) / max(1, len(tokens))) > 0.8:
    return {"score_letter": "Skip", "skip_reason": "hashtag_only_post", ...}

if len(clean_tokens) < 20:
    yellow_flags.append("insufficient_description")
    insufficient_description_flag = True
else:
    insufficient_description_flag = False
```

Эта проверка ловит посты вида `#react #frontend #remote #senior #рф #снг` — на них keyword-match даёт +2 за каждое попадание, итого A-grade, но писать туда некому. Без этого фильтра агент шлёт сообщения «в пустоту».

### Фаза 3 — Подсчёт баллов

Идём по чеклисту. Записывай вклад каждой строки в `breakdown`.

| # | Критерий | Баллы | Как проверять |
|---|----------|-------|---------------|
| 1 | must_match keyword #1 найден | +2 | Case-insensitive в title + description |
| 2 | must_match keyword #2 найден | +2 | Так же |
| 3 | must_match keyword #3 найден | +2 | Так же (если есть) |
| 4 | Каждый nice_to_have keyword | +1 за каждое | Так же |
| 5 | Уровень опыта совпадает | +1 | Вакансия ищет ≤ или ≈ нашему `experience_years_hint` |
| 6 | Зарплата ≥ min_usd_monthly | +2 | Только если зарплата указана. Конвертируй валюты. Не указана → 0 (без штрафа) |
| 7 | Известная компания | +1 | Узнаваемый бренд или well-funded стартап |
| 8 | Чёткие обязанности | +1 | Обязанности явно описаны |

**Что НЕ влияет:**
- Качество/длина описания: 0 баллов, не минус. Короткие TG-посты — норма.
- Зарплата не указана: 0 баллов, не минус.
- Требование по опыту выше нашего хинта: 0 баллов, не минус (просто нет +1 бонуса).

### Фаза 4 — Red flags (применяются ПОСЛЕ скоринга)

- Реально unpaid / volunteer / стажировка → `score_letter='Skip'`, `skip_reason='unpaid/intern'`.
- Требуется физическое присутствие, без remote → `score_letter='Skip'` (обычно уже ловится в 1.2).
- Совершенно другая специализация → `score_letter='Skip'`, `skip_reason='different_specialization'`.
- MLM-подобный язык, явный спам → `score_letter='Skip'`, `skip_reason='mlm_spam'`.

### Фаза 5 — Выставление оценки

Сначала по сумме баллов:

| Grade | Баллы | Смысл |
|-------|-------|-------|
| A | 8+ | Сильное совпадение |
| B | 5–7 | Хорошее совпадение |
| C | 3–4 | Слабое, но приемлемо |
| Skip | <3 | Нерелевантно |

Затем — корректировки:
- Если `insufficient_description_flag == True` И grade ∈ ("A", "B") → понизить до `C` (флаг `insufficient_description` уже в yellow_flags).
- Любой red_flag из Фазы 4 побеждает grade и ставит `Skip`.

Когда сомневаешься между B и Skip — ставь B. Лучше включить пограничную и дать человеку решить.

### Фаза 6 — yellow_flags (разрешённый список)

Если поставил yellow_flag — он ДОЛЖЕН быть из этого списка. Никаких произвольных строк (evaluate-and-initiate использует их для маршрутизации):

| flag | когда выставлять |
|------|-------------------|
| `stack_partial_match` | совпал только 1 must_match из ≥2, остальное nice_to_have или ничего |
| `seniority_mismatch` | вакансия явно ищет другой уровень (senior/lead, когда профиль middle, или наоборот) |
| `reject_pattern_soft_hit` | описание частично попадает в developer.reject_patterns (домен, условия), но не на 100% |
| `unstable_format` | заявлен remote, но в тексте намёки «изредка в офис», «офис опционально» |
| `insufficient_description` | автоматически ставится в Фазе 2 при clean_tokens < 20 |

**НЕ является yellow_flag:**
- Компания не названа — норма для TG.
- Зарплата не указана — норма.
- Короткое описание (но не пустое) — норма; флаг только при clean_tokens < 20.

### Фаза 7 — Определение языка и контакта

**Язык:**
- Преимущественно русский → `language: "ru"`.
- Преимущественно английский → `language: "en"`.
- Смешанный/неясный → проверь `source_language` если есть, иначе `"en"`.

**Контакт** этот скилл больше НЕ извлекает. Контакт уже лежит в `jobs.contact` (заполнен `telegram_scanner.py` или `scan-sources`). evaluate-and-initiate возьмёт оттуда.

## Пример вывода

```json
{
  "matched_position": "react-frontend",
  "score_letter": "A",
  "score_value": 11,
  "breakdown": [
    {"item": "must_match 'frontend' in title", "delta": 2},
    {"item": "must_match 'react' in description", "delta": 2},
    {"item": "nice_to_have 'TypeScript'", "delta": 1},
    {"item": "nice_to_have 'Next.js'", "delta": 1},
    {"item": "experience 3+ aligned", "delta": 1},
    {"item": "salary $4000 >= $2000", "delta": 2},
    {"item": "clear responsibilities", "delta": 1}
  ],
  "rationale": "Сильный React-стек, в коридоре зарплат, чёткие обязанности.",
  "red_flags": [],
  "yellow_flags": [],
  "remote_status": "confirmed",
  "language": "en",
  "skip_reason": null
}
```

Skip-пример:

```json
{
  "matched_position": null,
  "score_letter": "Skip",
  "score_value": 0,
  "breakdown": [],
  "rationale": "Пост состоит из одних хэштегов, осмысленных токенов 0.",
  "red_flags": [],
  "yellow_flags": [],
  "remote_status": "unclear",
  "language": "ru",
  "skip_reason": "hashtag_only_post"
}
```

## Ключевые принципы

1. **Чистая функция.** Никаких записей в БД, файлов, Gmail-черновиков, поиска контактов. Только вход → выход.
2. **Поле `score_letter`** (не `score`). Согласовано с evaluate-and-initiate.
3. **Hashtag-only фильтр в Фазе 2** — обязательная защита от пустых TG-баннеров.
4. **Позиция и роль — первично, стек — вторично.** «Frontend Developer» с Vue — всё равно frontend-роль.
5. **Никогда не штрафуй за отсутствие информации.** Неизвестная зарплата = 0 (не минус). Неясный remote = benefit of the doubt.
6. **Когда сомневаешься — включай.** Ставь B, дай человеку решить.
7. **Показывай математику.** Поле `breakdown` должно позволять любому проверить сумму, прочитав его.
