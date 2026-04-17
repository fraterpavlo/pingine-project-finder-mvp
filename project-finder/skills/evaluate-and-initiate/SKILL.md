---
name: evaluate-and-initiate
description: "Оценивает все вакансии со статусом status='new' (из любого источника — web-скан, telegram_scanner, inbox, ручной seed), присваивает класс A/B/C/Skip через evaluate-job, генерирует первое сообщение через generate-draft, затем либо автоматически инициирует первый контакт, либо эскалирует пограничные C-кейсы в TG-бот для ревью человеком, либо отклоняет. Это когнитивный слой, который обрабатывает любые вакансии, оказавшиеся в БД, независимо от того, как они туда попали."
---

# Навык Evaluate-and-Initiate

Ты — когнитивный слой, который берёт неоценённые вакансии из БД, скорит их через `evaluate-job`, генерирует первое сообщение через `generate-draft` и решает следующий шаг по каждой: автоматически инициировать контакт, спросить человека через TG-бот, либо отклонить. Ты никогда не сканируешь источники сам и никогда не шлёшь сообщения по сети — только пишешь intent-файлы, которые локальный демон `ops_applier.py` применяет к БД, а транспортом занимаются остальные локальные демоны.

## Определение пути

Найди директорию, содержащую `project-finder/`. Все пути ниже относительны от неё.

В Cowork-сессии корень обычно лежит как `/sessions/<session>/mnt/<workspace>/project-finder` — но не зашивай эти куски в код. Найди корень проверкой существования папки `skills/` и `scripts/`, начиная от текущей директории и поднимаясь вверх.

## Работа с БД из Cowork sandbox — ЧТЕНИЕ через snapshot, ЗАПИСЬ через intents

Cowork-sandbox монтирует рабочую папку через FUSE. SQLite в WAL-режиме на FUSE не работает (`disk I/O error`). Более того, copy-back файла `.sqlite` необратимо портит индексы и стирает данные локальных демонов. Поэтому строгое правило:

### НИКОГДА не открывай `project-finder/data/projectfinder.sqlite`

Ни для чтения, ни для записи. Даже не копируй его в sandbox.

### ЧТЕНИЕ — из `project-finder/data/snapshot.sqlite`

Локальный демон `ops_applier.py` раз в 60 секунд публикует актуальный read-only snapshot через `VACUUM INTO` (без `-wal`/`-shm`, режим DELETE). Snapshot можно открывать штатно:

```python
import sys, sqlite3, json, time, hashlib
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(...)            # ← вычислить (содержит skills/ + scripts/ + config/)
SCRIPTS = PROJECT_ROOT / "scripts"
SNAPSHOT = PROJECT_ROOT / "data/snapshot.sqlite"

# ВАЖНО: scripts/ должен быть в sys.path ПЕРЕД любым импортом pf_*.
# Без этого ImportError на самом первом import — частая ошибка.
sys.path.insert(0, str(SCRIPTS))
import pf_intents       # noqa: E402
import pf_policy        # noqa: E402

# Snapshot обязателен — без него работать нельзя
if not SNAPSHOT.exists():
    pf_intents.emit("notify_admin", {
        "summary": "evaluate-and-initiate: snapshot отсутствует",
        "message": "Файл data/snapshot.sqlite не найден. Проверь, работает ли ops_applier.",
        "urgency": "high",
        "type": "admin_alert",
    })
    return

# Проверка возраста snapshot (защита от read-stale).
# Если snapshot старше 5 минут — ops_applier завис, лучше отбой.
import os
snapshot_age_sec = time.time() - os.path.getmtime(SNAPSHOT)
if snapshot_age_sec > 300:
    pf_intents.emit("notify_admin", {
        "summary": "evaluate-and-initiate: snapshot устарел",
        "message": f"snapshot.sqlite не обновлялся {int(snapshot_age_sec)}s. ops_applier завис?",
        "urgency": "high",
        "type": "admin_alert",
    })
    return

conn = sqlite3.connect(f"file:{SNAPSHOT.as_posix()}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
```

### ЗАПИСЬ — через `pf_intents.emit(op, params)`

```python
key = pf_intents.emit("insert_outgoing", {
    "id": out_id,
    "conversation_id": conv_id,
    "job_id": job["id"],
    "developer_id": matched_position_profile_id,
    "channel": channel,
    "recipient": recipient,
    "subject": subject_or_None,
    "body": generated_text,
    "status": outgoing_status,
    "is_first_message": True,
    "confidence": confidence,
})
```

Под капотом — атомарная запись JSON-файла в `data/intents/pending/<uuid>.json`. ops_applier.py подхватит и применит в `pf_db.insert_outgoing(**params)`.

Допустимые операции — см. `pf_intents.ALLOWED_OPERATIONS`. Основные:
- `upsert_job` — обновить статус/match_json вакансии (альтернатива `set_job_status`).
- `set_job_status` — `(job_id, status, match=<dict>)`.
- `create_conversation` — новая нить диалога.
- `insert_outgoing` — первое сообщение в outgoing.
- `append_conversation_message` — зафиксировать наше исходящее в истории conversation.
- `insert_notification` — уведомление оператору (для `review_needed` с `outgoing_id`).
- `notify_admin` — системные алерты — сам подставляет chat_id.
- `state_set` — сохранить состояние прогона.
- `batch` — выполнить список операций в одной транзакции.

### Атомарная связка: conversation + outgoing + conv_message

`email_io`/`telegram_io` ищут существующие conversation в живой БД, а `dialogue-agent` подгружает историю через `list_conversation_messages`. Чтобы исключить ситуацию «outgoing есть, conv-сообщения нет», вставляй всё одним `batch`:

```python
pf_intents.emit_batch([
    {"operation": "create_conversation", "params": {...}},
    {"operation": "insert_outgoing", "params": {...}},
    {"operation": "append_conversation_message", "params": {...}},
])
```

ops_applier оборачивает batch в `BEGIN IMMEDIATE ... COMMIT`. Либо все три применяются, либо ни одна.

### Идемпотентность

Каждый intent получает uuid, который ops_applier хранит в `service_state.ops_applier.applied_keys` (rolling set, последние 2000). Дубль будет помечен `duplicate`. Также UNIQUE-констрейнты (на `jobs.url`, `incoming_messages.imap_message_id`) защитят на уровне схемы. Скилл можно безопасно перезапускать.

## Advisory-lock от параллельных запусков

Два прогона evaluate-and-initiate (ручной + cron в одну минуту) увидят одни и те же jobs со status='new' и оба эмитят `set_job_status + create_conversation + insert_outgoing` — HR получит ДВА письма. UNIQUE-индекса на `(job_id, channel, employer_contact)` для conversations нет. Лечится advisory-lock'ом через `service_state`:

```python
LOCK_KEY = "evaluate_and_initiate.lock"
LOCK_TTL_SEC = 30 * 60   # один прогон не должен длиться дольше 30 минут;
                          # если завис — следующий перехватит после TTL.

def _try_acquire_lock(conn):
    row = conn.execute("SELECT value_json FROM service_state WHERE key=?",
                       (LOCK_KEY,)).fetchone()
    if row and row["value_json"]:
        try:
            cur_lock = json.loads(row["value_json"])
            locked_at = datetime.strptime(cur_lock.get("locked_at",""),
                                          "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - locked_at).total_seconds()
            if age < LOCK_TTL_SEC:
                return False, cur_lock      # ещё бежит чужой прогон
        except Exception:
            pass   # битый лок — перехватываем
    return True, None

ok, current = _try_acquire_lock(conn)
if not ok:
    print(f"evaluate-and-initiate: lock held by {current}, exiting")
    return

# Захватываем
my_run_id = uuid.uuid4().hex
pf_intents.emit("state_set", {
    "key": LOCK_KEY,
    "value": {
        "locked_at": utcnow_iso(),
        "run_id": my_run_id,
        "source": "evaluate-and-initiate",
    },
})
# Внимание: lock эмитится через intent → ops_applier применит через ~5с.
# Окно гонки сужается, но не нулевое. Полное решение — UNIQUE-индекс на
# conversations(job_id, channel, status='active'), см. §7.3 ревью.

# ... основной цикл ...

# В финале — снять лок:
pf_intents.emit("state_set", {"key": LOCK_KEY, "value": None})
```

## Вход

```python
jobs_to_process = [dict(r) for r in conn.execute(
    "SELECT * FROM jobs WHERE status = 'new' ORDER BY discovered_at ASC"
)]
```

Эти вакансии могли прийти из любого источника — тебе НЕ нужно знать, из какого. Web-скан, локальный `telegram_scanner.py`, ручной seed — всё приходит сюда с одним и тем же `status='new'`.

## Конфиг-файлы для чтения (в порядке)

1. `project-finder/config/positions.json` — позиции, которые кандидат ищет.
2. `project-finder/config/scoring-rules.md` — пороги A/B/C/Skip, red flags, yellow flags.
3. `project-finder/config/writing-style.md` — тон, запрещённые клише.
4. `project-finder/config/auto-reply-config.json` — `first_message_policy`, rate limits.
5. `project-finder/config/developers/*.json` — профили (см. ниже выбор).
6. `project-finder/config/templates/cover-letter-en.md` и `cover-letter-ru.md` — базовые шаблоны.

## Выбор `developer_id` для вакансии

Раньше переменная `matched_position_profile_id` подразумевалась известной — но неоткуда. Теперь явный алгоритм:

```python
import os, json

profiles = []
dev_dir = PROJECT_ROOT / "config" / "developers"
for fpath in sorted(dev_dir.glob("*.json")):
    with fpath.open(encoding="utf-8") as f:
        prof = json.load(f)
    if not prof.get("active", True):
        continue
    profiles.append((fpath.stem, prof))

def pick_developer_for_position(matched_position: str):
    """
    Из всех active профилей выбрать тот, у которого
    position_matching.applicable_position_ids содержит matched_position.
    Если несколько — приоритет по position_matching.priority_order
    (чем ближе matched_position к началу priority_order — тем выше).
    """
    candidates = []
    for dev_id, prof in profiles:
        pm = prof.get("position_matching") or {}
        applicable = pm.get("applicable_position_ids") or []
        if matched_position in applicable:
            order = pm.get("priority_order") or []
            try:
                rank = order.index(matched_position)
            except ValueError:
                rank = 999
            candidates.append((rank, dev_id, prof))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1], candidates[0][2]   # (developer_id, profile_dict)
```

Если функция вернула `(None, None)` — `set_job_status('error_no_profile')` через intent, логируй в `errors[]`, иди к следующей вакансии.

## Алгоритм

Для каждой вакансии в `jobs_to_process` выполняй три фазы. Если шаг падает по одной вакансии — логируй в локальный `errors[]` и переходи к следующей. Никогда не прерывай весь прогон из-за одной сломанной вакансии.

---

### Фаза 1 — Оценка

Вызови `project-finder/skills/evaluate-job/SKILL.md` с входным словарём `{title, url, description, source_id, discovered_at, contact, company, language}`. Скилл вернёт СЛОВАРЬ строго по контракту:

```python
{
  "matched_position": "react-frontend" | None,
  "score_letter": "A" | "B" | "C" | "Skip",
  "score_value": int,
  "breakdown": [...],
  "rationale": "...",
  "red_flags": [...],
  "yellow_flags": [...],     # только из разрешённого списка (см. evaluate-job)
  "remote_status": "confirmed" | "unclear" | "denied",
  "language": "ru" | "en",
  "skip_reason": "..." | None,
}
```

**Поле называется `score_letter`, не `score`.** Если получил `score` — значит evaluate-job устарел; скорректируй (внеси багфикс).

evaluate-job УЖЕ применяет фильтр «hashtag-only post» в Фазе 2 — повторно его делать не надо.

Вычисли флаг `borderline`:

```python
borderline = (
    score_letter == "C"
    or (45 <= score_value <= 55)
    or len(yellow_flags) > 0
)
```

Запиши результат в `match` для intent'а:

```python
match = {
    "score_letter": score_letter,
    "score_value": score_value,
    "matched_position": matched_position,
    "breakdown": breakdown,
    "rationale": rationale,
    "red_flags": red_flags,
    "yellow_flags": yellow_flags,
    "borderline": borderline,
    "remote_status": remote_status,
}
```

---

### Фаза 2 — Маршрутизация по классу

Для ЛЮБОЙ подходящей вакансии (A/B/C) генерируем первое сообщение в Фазе 3. Различие между «авто» и «ревью» — только через `outgoing_messages.status`:

- `ready` → демон отправляет.
- `needs_review` → демон не трогает; оператору уходит `notifications(type='review_needed')` с `outgoing_id` и inline-кнопками ✅/✏️/❌.

| score_letter | borderline | `jobs.status` | `outgoing.status` | notification |
|--------------|-----------|---------------|-------------------|--------------|
| A | false | `outreach_queued` | `ready` | — |
| A | true  | `outreach_queued` | `needs_review` | `review_needed` |
| B | false | `outreach_queued` | `ready` | — |
| B | true  | `outreach_queued` | `needs_review` | `review_needed` |
| C | false | `outreach_queued` | `needs_review` | `review_needed` |
| C | true  | `outreach_queued` | `needs_review` | `review_needed` |
| Skip | —   | `rejected` | (не создаём) | — |

Применяй через единую функцию `pf_policy.decide_outgoing_status` (она же учтёт `auto-reply-config.first_message_policy.default`, `developer.auto_reply_settings.auto_send_first_message`, `job.match.auto_send`):

```python
import json
with (PROJECT_ROOT / "config" / "auto-reply-config.json").open(encoding="utf-8") as f:
    auto_cfg = json.load(f)

outgoing_status = pf_policy.decide_outgoing_status(
    score_letter=score_letter,
    borderline=borderline,
    developer=developer_profile,
    global_cfg=auto_cfg,
    job_override=(match.get("auto_send")),
    confidence=confidence,   # из generate-draft, считается ниже
)
# вернёт "ready" | "needs_review", либо None для Skip
```

LOW confidence (от generate-draft) ВСЕГДА уходит в `needs_review` независимо от policy — это уже зашито в `decide_outgoing_status`.

---

### Фаза 3 — Генерация первого сообщения

Вызови `project-finder/skills/generate-draft/SKILL.md` со входом:

```python
# Подготовь previous_first_message_body — для anti-template (P1-10)
prev_row = conn.execute(
    "SELECT body FROM outgoing_messages "
    "WHERE developer_id=? AND is_first_message=1 AND status='sent' "
    "ORDER BY sent_at DESC LIMIT 1",
    (developer_id,)
).fetchone()
previous_first_message_body = prev_row["body"] if prev_row else None

draft_input = {
    "job": {
        "id": job["id"],
        "title": job["title"],
        "description": job.get("description") or "",
        "company": job.get("company"),
        "language": match.get("language") or "en",
        "matched_position": matched_position,
        "score_letter": score_letter,
        "yellow_flags": yellow_flags,
    },
    "developer": developer_profile,
    "channel": channel,    # см. ниже
    "previous_first_message_body": previous_first_message_body,
}
```

Скилл вернёт словарь:

```python
{
  "subject": "..." | None,
  "body": "...",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "facts_used": [...],
  "placeholders_left": [...],
  "personalization_facts": [...],
}
```

#### Канал и нормализация контакта

Канал и получатель выводятся из `jobs.contact`:

- email-вид (`name@domain.tld`) → `channel='email'`, `recipient = email.utils.parseaddr(contact)[1].strip().lower()`. Subject — из `draft["subject"]`.
- начинается с `@` или TG-username → `channel='telegram'`, `recipient = '@' + name.lower().strip().lstrip('@')`. Subject игнорируем.
- иначе → `set_job_status('error_no_contact')`, пропускай.

**Зачем нормализация:** `email_io.get_known_email_contacts` и `telegram_io.find_conversation` ищут conversations по `employer_contact` через сравнение в lower-case. Если ты сохранил `Fratter Pavlo <fraterpavlo@gmail.com>` — слушатель не найдёт диалог при ответе HR.

---

### Фаза 3а — Один intent на вакансию (batch)

Всё, что этот скилл пишет в БД по одной вакансии, — одним `emit_batch`:

```python
import uuid
conv_id = f"conv-{uuid.uuid4().hex[:8]}"
out_id  = f"out-{uuid.uuid4().hex[:8]}"
notif_id = f"notif-{uuid.uuid4().hex[:8]}"

ops = [
    {"operation": "set_job_status", "params": {
        "job_id": job["id"],
        "status": "outreach_queued" if score_letter != "Skip" else "rejected",
        "match": match,
    }},
]

if score_letter != "Skip":
    ops.append({"operation": "create_conversation", "params": {
        "id": conv_id,
        "job_id": job["id"],
        "developer_id": developer_id,
        "channel": channel,
        "employer_contact": normalized_contact,
        "status": "active",
    }})
    ops.append({"operation": "insert_outgoing", "params": {
        "id": out_id,
        "conversation_id": conv_id,
        "job_id": job["id"],
        "developer_id": developer_id,
        "channel": channel,
        "recipient": normalized_contact,
        "subject": draft.get("subject"),
        "body": draft["body"],
        "status": outgoing_status,
        "is_first_message": True,
        "confidence": draft["confidence"],
    }})
    ops.append({"operation": "append_conversation_message", "params": {
        "conversation_id": conv_id,
        "msg": {
            "direction": "outgoing",
            "content": draft["body"],
            "outgoing_id": out_id,
            "confidence": draft["confidence"],
            "status": outgoing_status,
        },
    }})
    if outgoing_status == "needs_review":
        ops.append({"operation": "insert_notification", "params": {
            "id": notif_id,
            "type": "review_needed",
            "urgency": "high" if draft["confidence"] == "LOW" else ("normal" if borderline else "low"),
            "job_id": job["id"],
            "job_title": job["title"],
            "conversation_id": conv_id,
            "outgoing_id": out_id,
            "reason": ", ".join(yellow_flags) or ("borderline" if borderline else "first_message_always_review"),
            "summary": f"[{score_letter} {score_value}] {job['title']} → {channel}:{normalized_contact}",
            "recipient": "admin",
            "telegram_chat_id": admin_chat_id,
            "message_sent": review_message_text,
            "telegram_status": "pending",
        }})

pf_intents.emit_batch(ops, source="evaluate-and-initiate")
```

`telegram_notifier` прикрепит к уведомлению inline-кнопки ✅/✏️/❌ благодаря `outgoing_id`.

**Колонок `vacancy_id`, `severity`, `body`, `actions`, `meta_json` в таблице `notifications` НЕТ.** Допустимые поля — те, что принимает `pf_db.insert_notification`.

Где взять `admin_chat_id` — см. блок «Получение admin_chat_id» в конце.

---

## Финализация

После обработки всех вакансий собери отчёт.

### Счётчики

- `total_evaluated`
- `by_grade` — `{"A": n, "B": n, "C": n, "Skip": n}`
- `auto_sent` — `outgoing.status='ready'`
- `awaiting_review` — `outgoing.status='needs_review'` + уведомление
- `rejected` — Skip
- `skip_reasons` — словарь категорий
- `borderline_reasons` — словарь yellow-flag
- `errors[]` — `{job_id, phase, message}`
- `per_job_decisions[]` — короткая строка на вакансию
- `run_started_at`, `run_finished_at`

### 1. Сохранить состояние (один intent)

```python
pf_intents.emit("state_set", {
    "key": "evaluate_and_initiate",
    "value": {
        "last_run_at": run_finished_at,
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "total_evaluated": total_evaluated,
        "by_grade": by_grade,
        "auto_sent": auto_sent,
        "awaiting_review": awaiting_review,
        "rejected": rejected,
        "skip_reasons": skip_reasons,
        "borderline_reasons": borderline_reasons,
        "errors": errors,
        "per_job_decisions_last_run": per_job_decisions,
    },
}, source="evaluate-and-initiate.summary")
```

### 2. Освобождение advisory-lock

```python
pf_intents.emit("state_set", {"key": "evaluate_and_initiate.lock", "value": None})
```

### 3. Cycle-summary уведомление с детерминированным idempotency_key

Чтобы избежать дублей при ручном перезапуске в первые 60 секунд (snapshot ещё не покажет только что отправленный summary), используем детерминированный ключ — окно 30 минут:

```python
# округляем до 30-минутных окон
window_minute = (datetime.now(timezone.utc).minute // 30) * 30
key_seed = datetime.now(timezone.utc).strftime(f"%Y-%m-%dT%H:{window_minute:02d}")
deterministic_key = hashlib.sha1(f"cycle_summary-{key_seed}".encode()).hexdigest()

pf_intents.emit("insert_notification", {
    "id": notif_id,
    "type": "cycle_summary",
    "urgency": "low" if len(errors) == 0 else "normal",
    "summary": f"cycle by {by_grade}, auto={auto_sent} review={awaiting_review} skip={rejected}",
    "recipient": "admin",
    "telegram_chat_id": admin_chat_id,
    "message_sent": summary_text,
    "telegram_status": "pending",
}, source="evaluate-and-initiate.cycle-summary",
   idempotency_key=deterministic_key)   # КЛЮЧЕВОЙ параметр — повторный emit с тем же
                                         # key будет помечен duplicate в ops_applier
```

**Структура `summary_text`:**

```
🤖 ProjectFinder — итог цикла evaluate-and-initiate
⏱ {run_finished_at_human}   ⏳ длительность: {duration_seconds}s

📊 Обработано: {total_evaluated}
   • A: {n_A}   • B: {n_B}   • C: {n_C}   • Skip: {n_Skip}

✅ Авто-отправлено первых сообщений: {auto_sent}
📝 На ревью человеку (review_needed): {awaiting_review}
❌ Отклонено: {rejected}
⚠️ Ошибок: {len(errors)}

— Причины отклонения (Skip):
{render_dict(skip_reasons)}

— Причины ревью (yellow flags):
{render_dict(borderline_reasons)}

— Решения по вакансиям (первые 15):
{render_lines(per_job_decisions[:15])}
```

Правила:
- лимит Telegram — 4096 символов. Если превысили — обрежь `per_job_decisions` до 5 строк, добавь хвост «…сообщение усечено, полный отчёт — в service_state».
- `errors[:5]` — если ошибок >5, добавь «…и ещё N ошибок в service_state».
- если `total_evaluated == 0` — ровно одно тело: `🤖 ProjectFinder — итог цикла\n⏱ {now}\nНовых вакансий (status='new') в этом цикле не было.`

Если `notifications-config.json` отсутствует или TG-бот выключен — логируем warning, шаги 1 и 2 всё равно выполняем.

### 4. Консольный summary

```
Обработано: {total_evaluated} вакансий
- A: {n}, B: {n}, C: {n}, Skip: {n}
- Авто-отправка (outgoing status=ready): {auto_sent}
- На ревью (needs_review + notification): {awaiting_review}
- Отклонено: {rejected}
- Ошибки: {len(errors)}
```

## Получение `admin_chat_id`

Не угадывай. Прочитай конфиг:

```python
with (PROJECT_ROOT / "config" / "notifications-config.json").open(encoding="utf-8") as f:
    nconf = json.load(f)

admin_chat_id = None
for r in nconf.get("recipients", []):
    if r.get("is_admin") is True:
        cid = r.get("telegram_chat_id")
        if cid and "PASTE" not in str(cid):
            admin_chat_id = cid
            break
if admin_chat_id is None:
    for r in nconf.get("recipients", []):
        cid = r.get("telegram_chat_id")
        if cid and "PASTE" not in str(cid):
            admin_chat_id = cid
            break
```

Если всё ещё None — emit `notify_admin` (он сам разберётся с fallback) и продолжай без notifications.

## Алерты для оператора

Для системных проблем — intent `notify_admin` (`pf_db.notify_admin` сам подставит chat_id):

```python
pf_intents.emit("notify_admin", {
    "summary": "evaluate-and-initiate: snapshot отсутствует",
    "message": "Файл data/snapshot.sqlite не найден. Проверь, работает ли ops_applier.",
    "urgency": "high",
    "type": "admin_alert",
})
```

## Жёсткие правила (чего этот навык НИКОГДА не делает)

- НЕ открывает `projectfinder.sqlite` ни для чтения, ни для записи. Чтение — только `snapshot.sqlite`. Запись — только через `pf_intents.emit`.
- НЕ копирует `.sqlite` в sandbox.
- НЕ сканирует web-источники. Этим занимается `scan-sources`.
- НЕ читает и не парсит ответы HR. Этим занимается `process-dialogues` через `dialogue-agent`.
- НЕ шлёт email, Telegram-сообщения или сообщения от бота напрямую. Только intent'ы. Транспорт — локальные демоны.
- НЕ выдумывает факты о кандидате. Если плейсхолдер требует данные, которых нет, — оставляй плейсхолдером, и confidence уйдёт в LOW.
- НЕ использует «we / our team / agency / команда». Только первое лицо единственного числа.
- НЕ запускается параллельно — advisory-lock защищает от двойной отправки.
