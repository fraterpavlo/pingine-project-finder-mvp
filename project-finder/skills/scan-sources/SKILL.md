---
name: scan-sources
description: "Сканирует веб-источники вакансий из sources.json, применяет ЛЁГКИЙ фильтр по ключевым словам и эмитит intent-файлы для вставки подходящих вакансий в jobs(status='new'). Используй, когда пайплайну нужно обнаружить новые веб-объявления. Telegram-источники ЗДЕСЬ не обрабатываются — их непрерывно сканирует локальный демон telegram_scanner.py. Оценка (A/B/C/Skip) ЗДЕСЬ не делается — это задача задачи evaluate-and-initiate."
---

# Scan Sources — скилл обнаружения веб-вакансий

Ты — агент обнаружения. Твоя задача:
1. Найти вакансии на включённых веб-источниках.
2. Применить ЛЁГКИЙ фильтр (ключевые слова `must_match` / `role_indicators` из positions.json).
3. Эмитить intent-файлы для вставки подходящих в `jobs(status='new')`.

Ты НЕ оцениваешь (никаких A/B/C/Skip). Ты НЕ генерируешь сообщения. Ты НЕ открываешь `projectfinder.sqlite` напрямую — это запрещено для Cowork-скиллов из-за FUSE.

## Разрешение путей

Найди каталог, содержащий `project-finder/`. Все пути ниже относительны от него.

## Работа с БД из Cowork sandbox

**Запрет:** не открывай `data/projectfinder.sqlite` напрямую. Только `data/snapshot.sqlite` для чтения и `pf_intents` для записи.

**Чтение** (для дедупа и проверки seen):

```python
import sys, sqlite3
from pathlib import Path

PROJECT_ROOT = Path(...)            # ← вычисли (содержит skills/ + scripts/ + config/)
SCRIPTS = PROJECT_ROOT / "scripts"
SNAPSHOT = PROJECT_ROOT / "data/snapshot.sqlite"

sys.path.insert(0, str(SCRIPTS))    # ВАЖНО: иначе ImportError
import pf_intents

if not SNAPSHOT.exists():
    pf_intents.emit("notify_admin", {
        "summary": "scan-sources: snapshot отсутствует",
        "message": "Файл data/snapshot.sqlite не найден. Проверь, работает ли ops_applier.",
        "urgency": "high",
        "type": "admin_alert",
    })
    return

conn = sqlite3.connect(f"file:{SNAPSHOT.as_posix()}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

def has_seen_web(url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM seen_message_ids WHERE source='web' AND external_id=?",
        (url,)
    ).fetchone()
    if row:
        return True
    # Также проверяем сам jobs (на случай, если seen-запись потерялась):
    row = conn.execute("SELECT 1 FROM jobs WHERE url=?", (url,)).fetchone()
    return row is not None
```

**Запись** — только через `pf_intents.emit_batch([upsert_job, state_set("seen.web."+url, ...)])`.

**Внимание:** `seen_message_ids` через intents пишется как `state_set` или через специальную операцию. На текущий момент `pf_intents.ALLOWED_OPERATIONS` НЕ содержит явной операции `mark_seen`, но `upsert_job` уже даёт UNIQUE-защиту (на `jobs.url`) — этого достаточно для дедупа на уровне jobs. Запись в `seen_message_ids` для веб-вакансий пока опускаем; она нужна была telegram-сканеру для случаев, когда пост ещё не превратился в job.

## Конфигурация

Прочитай ОДИН раз в начале:

1. `project-finder/config/sources.json` — список источников.
2. `project-finder/config/positions.json` — позиции, которые мы ищем.

Snapshot — для дедупа (см. выше).

## Алгоритм

### Шаг 1: Загрузить и отфильтровать источники

- Прочитай `sources.json`, оставь только `enabled: true`.
- **Пропусти любой источник с `scan_method == "telegram"`** — их обрабатывает локальный `telegram_scanner.py`.
- Отсортируй по приоритету: high → medium → low.
- Прочитай `positions.json`.
- Через snapshot собери `has_seen_web()` для проверки новизны URL.

### Шаг 2: Просканировать каждый источник

Для каждого включённого веб-источника выполни поиск по `scan_method`:

#### Метод: `web_search`

Для каждой записи в `search_queries`:

1. Комбинируй с доменом источника: `site:weworkremotely.com react developer 2026`.
2. Если `language: "ru"` — оставляй на русском.
3. По возможности добавляй текущий год.
4. Собери URL/заголовки/сниппеты с первых 1–2 страниц выдачи.

**Подсказки:**
- **HackerNews:** ищи свежий «Ask HN: Who is hiring» за текущий месяц.
- **hh.ru:** сниппеты часто содержат зарплату/опыт/город.
- **RemoteOK / WeWorkRemotely:** прямые ссылки `remoteok.com/remote-jobs/...` / `weworkremotely.com/remote-jobs/...`.

#### Метод: `web_fetch`

- Используй WebFetch.
- Если упал — фолбэк на `web_search` для этого источника.

#### Метод: `browser`

- Если браузерных инструментов нет → ПРОПУСТИ источник, залогируй warning.
- В основном для LinkedIn.

### Шаг 3: Извлечь данные

Для каждого найденного объявления:

```json
{
  "title": "Senior React Developer",
  "url": "https://...",
  "company": "Example Corp",
  "short_description": "Краткое описание (200-300 chars)",
  "raw_description": "",
  "source_id": "wwr",
  "source_language": "en",
  "discovered_date": "2026-04-17"
}
```

### Шаг 4: Лёгкий фильтр по ключевым словам

Подготовь множество ключей ОДИН раз:
- из `positions.json` собери все `keywords.must_match[]` и `keywords.role_indicators[]` активных позиций;
- lowercase, в set.

Для каждой сырой вакансии (title + short_description + raw_description, объединённые в lowercase) проверь содержание ХОТЯ БЫ ОДНОГО ключа. Если нет — выбрасывай, не emit'и.

### Шаг 5: Дедупликация

`if has_seen_web(url): пропусти`.

### Шаг 6: Загрузить полные описания (только для не-дубликатов)

- WebFetch → `raw_description`.
- Если упал — оставь `short_description`.

### Шаг 7: Эмит intent-ов в БД

Для каждой новой вакансии:

```python
pf_intents.emit("upsert_job", {
    "id": job_id,
    "source_id": source_id,
    "url": url,
    "channel": "web",
    "title": title,
    "company": company,
    "description": raw_description or short_description,
    "contact": extracted_email_or_None,
    "discovered_at": now_iso,
    "raw": full_record,
    "status": "new",
}, source="scan-sources")
```

UNIQUE-индекс на `jobs.url` обеспечит идемпотентность на стороне ops_applier — повторный emit того же URL не приведёт к дублю.

### Шаг 8: Сохранить состояние прогона

```python
pf_intents.emit("state_set", {
    "key": "scan_sources",
    "value": {
        "last_run_at": now_iso,
        "sources_scanned": n_ok,
        "sources_failed": n_fail,
        "jobs_emitted": n_emit,
        "jobs_filtered_out": n_filter,
        "duplicates": n_dup,
        "errors": errors_list[:20],
    },
}, source="scan-sources.summary")
```

## Обработка ошибок

- Источник не отвечает → ЗАЛОГИРУЙ, ПРОПУСТИ, продолжай.
- WebFetch блокируется → попробуй WebSearch как фолбэк.
- `browser` без браузера → пропусти с warning.
- **НИКОГДА не останавливай весь скан из-за одного источника.**

## Резюме прогона

Короткий отчёт в консоль:
- Сколько источников ОК / упало / пропущено (Telegram).
- Сырых хитов / отфильтровано / дубликатов / эмиттировано intent'ов.
- Список новых (title + источник + URL) для быстрого просмотра.

## Важные правила

- НИКОГДА не открывай `data/projectfinder.sqlite` напрямую.
- НИКОГДА не вызывай `pf_db.upsert_job` или `pf_db.mark_seen` — только через `pf_intents.emit`.
- НЕ оценивай вакансии — это задача `evaluate-and-initiate`.
- НЕ генерируй сообщения — это задача `evaluate-and-initiate` через `generate-draft`.
- Уважай `max_vacancy_age_days` (предпочитай свежие за последние 7 дней).
- Если результат явно не объявление (статья, гайд, профиль компании) — пропускай.
