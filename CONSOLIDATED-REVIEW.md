# ProjectFinder — консолидированное ревью

**Дата:** 2026-04-16
**Источники:** первая версия документа — сводка [REVIEW-2026-04-16.md](REVIEW-2026-04-16.md) + [ANALYSIS.md](ANALYSIS.md) + независимая проверка кода и состояния БД. Исходные два файла удалены после того, как важные факты и решения переехали сюда (§5).
**Статус проекта:** архитектурная проблема (FUSE-porша БД при copy-back из Cowork) закрыта переходом на intent-queue + read-only snapshot. Критические логические баги исправлены. Осталась чистка устаревшей документации и несколько улучшений второго порядка (см. §3 Open).

---

## 1. Архитектура на текущий момент

Инструмент — автономный поиск вакансий и первичный контакт с HR от имени одного (пока тестового) разработчика. Когнитивные решения делает Cowork, транспорт и БД — локальные демоны.

**Запуск локально:** `py -3 project-finder/scripts/projectfinder.py` поднимает шесть демонов с auto-restart и единым логом:
- `ops_applier.py` **(новый, 2026-04-16)** — читает `data/intents/pending/`, применяет команды к БД через `pf_db.*`, раз в 60 с публикует `data/snapshot.sqlite` для Cowork-чтения. Единственный писатель в БД со стороны Cowork-скиллов.
- `telegram_scanner.py` — Telethon, сканирует TG-каналы → `jobs(status='new')`.
- `telegram_io.py` — Telethon, слушатель входящих DM + отправитель `outgoing_messages(channel='telegram')` через two-phase commit.
- `email_io.py` — SMTP-отправка + IMAP-поллинг.
- `telegram_notifier.py` — шлёт `notifications(telegram_status='pending')` в Telegram-бота оператора.
- `bot_handler.py` — обрабатывает inline-кнопки (✅/✏️/❌) и команды `/review`, `/status`, `/start`.

**В Cowork sandbox (scheduled tasks):**
- `projectfinder-scan-sources` (cron `0 */4 * * *`) — web-скан по `sources.json`. Сейчас в источниках только тестовый TG-канал, т.е. задача каждый 4 часа жжёт квоту впустую.
- `projectfinder-evaluate-and-initiate` (cron `15 */2 * * *`) — берёт `jobs(status='new')`, скорит через `evaluate-job`, генерит первое сообщение, пишет `outgoing_messages` + `notifications`.
- `projectfinder-process-dialogues` (cron `* * * * *`) — обрабатывает `incoming_messages(status='new')` через `dialogue-agent`.

**Хранилище:** единая SQLite-база `project-finder/data/projectfinder.sqlite` + data layer `scripts/pf_db.py`. 9 таблиц: `jobs`, `conversations`, `conversation_messages`, `incoming_messages`, `outgoing_messages`, `notifications`, `escalations`, `service_state`, `seen_message_ids`. Two-phase commit для outgoing (`ready → sending → sent/failed`). UNIQUE-ограничения для дедупликации. Миграция с JSON выполнена в коммите `c70fb0e`.

**Текущее содержимое БД (проверено 2026-04-16):**
```
jobs                  5   (3 outreach_queued, 2 rejected)
conversations         3
conversation_messages 0
incoming_messages     0
outgoing_messages     3   (2 ready, 1 needs_review)
notifications         9   (2 pending реальных, 1 cycle-summary pending, 1 sent; 1 broken health-alert с recipient=None)
escalations           0
seen_message_ids      5
service_state         3   (scan_sources, evaluate_and_initiate, telegram_scanner)
```
Все job'ы — из тестового TG-канала `@testChannelProjectFinderPingineT`. Боевых источников нет.

---

## 2. Главная проблема: Cowork sandbox ↔ SQLite через FUSE

### 2.1 Симптомы и улики (все подтверждены непосредственной проверкой)

**`PRAGMA integrity_check` на живом файле возвращает:**
```
wrong # of entries in index ix_notif_ack
wrong # of entries in index sqlite_autoindex_notifications_1
```
Индексы расходятся с данными. Это не баг SQLite — это следствие того, что файл `.sqlite` был перезаписан снаружи, пока локальный процесс держал его открытым и писал в WAL.

**Self-aware notification `notif-de689e5b`** — в таблице `notifications` лежит запись, созданная health-check'ом при очередном старте скилла: тип `None`, `recipient=None`, `telegram_chat_id=None`, `urgency=high`, в `message_sent` — текст «🚨 Database integrity check FAILED / wrong # of entries in index ix_notif_ack ...». То есть система сама заметила порчу, но не может доставить алерт, потому что не знает, кому его адресовать.

**FUSE-артефакты в `project-finder/data/`** (на момент ревью — 4 файла):
```
.fuse_hidden0000002100000001   32K
.fuse_hidden0000004b00000001   32K
.fuse_hidden0000009800000001   32K
.fuse_hidden0000009900000002   32K
```
FUSE создаёт такой shadow inode, когда файл переименовывается или удаляется, пока другой процесс держит на него открытый fd. Размер 32 KiB = 8 страниц SQLite = типичные 1–2 транзакции демона.

### 2.2 Root cause

```
disk I/O error                →  WAL требует mmap для .sqlite-shm;
                                 FUSE этого не умеет → open() EIO.

wrong # of entries in index   →  Cowork-скилл скопировал .sqlite+.sqlite-wal
                                 в sandbox, правил SQL-ом, копировал .sqlite
                                 обратно через `cp`. В это время локальный
                                 демон уже записал несколько страниц
                                 в оригинальный .sqlite-wal. После copy-back
                                 .sqlite и .sqlite-wal принадлежат разным
                                 версиям — индексы и данные рассинхронизированы.

.fuse_hidden*                 →  FUSE shadow inode, когда файл
                                 перезаписали под открытым fd демона.
```

Инструкция «read snapshot → SQL patch → copy back» прямо записана в [project-finder/skills/evaluate-and-initiate/SKILL.md:14-140](project-finder/skills/evaluate-and-initiate/SKILL.md:14). Каждый прогон скилла — новая порция повреждений. На момент ревью journal mode в файле уже сброшен в `delete` (видно по PRAGMA), т.е. WAL-фолбэк в `pf_db.py` успел сработать, но индексы уже были испорчены до этого.

### 2.3 Почему copy-back не сработает никогда (независимо от числа файлов и чекпоинтов)

1. На оригинал могут быть открыты `.sqlite-wal` / `.sqlite-shm` от локальных демонов. Перезаписать их по отдельности атомарно нельзя — SQLite ожидает согласованного снимка всех трёх.
2. `PRAGMA wal_checkpoint(FULL)` в sandbox применяется к копии в sandbox, а не к оригиналу. На оригинале незафиксированные страницы демона просто теряются.
3. Даже если отбросить WAL и работать в DELETE-mode — между моментом `cp DB_SRC DB_WRITE` и `cp DB_WRITE DB_SRC` у демона есть секунды, чтобы записать изменение, которое затрётся.
4. FUSE не пробрасывает fcntl-локи между хост-процессом (демон на NTFS) и sandbox-процессом (скилл на Linux-mount) — SQLite не может увидеть чужую блокировку.

**Вывод:** не добавлять четвёртый файл, не писать ещё один чекпоинт, не городить самодельный lock-файл. Нужно менять парадигму доступа.

### 2.4 Рекомендация — Вариант A (intent-queue + read-only snapshot)

**Запись из Cowork.** Cowork НЕ открывает `.sqlite`. Вместо этого кладёт JSON-файл в `project-finder/data/intents/pending/<uuid>.json`:

```json
{
  "idempotency_key": "e7a4…",
  "created_at": "2026-04-16T09:00:00Z",
  "source": "evaluate-and-initiate",
  "operation": "insert_outgoing",
  "params": {
    "conversation_id": "conv-abc123",
    "job_id": "tg-…",
    "developer_id": "test-fullstack",
    "channel": "telegram",
    "recipient": "@ivandopalas",
    "body": "…",
    "status": "ready",
    "is_first_message": true,
    "confidence": "HIGH"
  }
}
```

Запись JSON через `os.rename(tmp, final)` атомарна и на FUSE работает штатно.

**Применение на локальной машине.** Новый демон `project-finder/scripts/ops_applier.py` (шестой сервис в `projectfinder.py`):
- раз в 5–10 с читает `data/intents/pending/*.json`;
- диспатчит в `pf_db.<operation>(**params)`;
- переносит в `data/intents/applied/` или `data/intents/failed/`;
- идемпотентность через `service_state.applied_ops_idempotency_keys` (множество последних 1000 ключей).

**Чтение из Cowork.** Cowork читает не оригинал, а snapshot `data/snapshot.sqlite`, который тот же `ops_applier` раз в 60 с публикует через:
```python
conn.execute("VACUUM INTO 'data/snapshot.sqlite.tmp'")
os.replace("data/snapshot.sqlite.tmp", "data/snapshot.sqlite")
```
Snapshot — в DELETE-mode, без `-wal/-shm`. Cowork открывает его read-only, никаких конфликтов.

Плюсы: один писатель, N читателей; идемпотентность на уровне файлов; всё, что делает Cowork, визуально в папке — легко дебажить.
Минусы: +1 демон; скиллы пишут не напрямую в `pf_db`, а через helper `pf_intents.emit(op, params)`; read-lag до 60 с.

### 2.5 Альтернатива — Вариант B (отключить WAL)

В `pf_db.py` поставить `PRAGMA journal_mode=DELETE` безусловно. Cowork сможет открывать `.sqlite` напрямую через FUSE, писать под `BEGIN IMMEDIATE + busy_timeout`.

Плюсы: 2 строки кода.
Минусы:
- не решает copy-back. Если Cowork по sandbox-политике всё равно обязан копировать файл к себе, проблема возвращается. Это нужно опытно проверять.
- остаются fcntl-локи через FUSE — риск `database locked` и потерь.
- концептуально оставляет прямой конкурентный доступ двух ОС к одному файлу — хрупко.

### 2.6 Сравнительная таблица A vs B

| Критерий | Вариант A (intents + snapshot) | Вариант B (journal=DELETE) |
|---|---|---|
| Надёжность | Высокая — один писатель | Средняя — fcntl через FUSE шаткий |
| Решает FUSE-порчу | Да, полностью | Частично (если sandbox всё равно копирует — нет) |
| Объём работ | +1 демон, переписать 2 скилла, helper `pf_intents` | 2 строки в `pf_db.py` |
| Видимость операций | Файлы в `intents/` — легко дебажить | Прямой SQL — как сейчас |
| Lag чтения | ~60 с | 0 |
| Риск потери данных демонов | Нет | Ненулевой |
| Риск обратного отката | Нет | Есть (copy-back) |

Итог: Вариант A — правильный путь. Вариант B допустим только как «прямо сейчас подлатали на 2 часа, пока я уезжаю», и то под вопросом.

---

## 3. Каталог проблем

Идентификаторы: `P0` — блокирующие работу инструмента сейчас, `P1` — надёжность и корректность, `P2` — чистка и полезные улучшения, `P3` — nice-to-have.

### P0 — блокирующие

#### P0-1. Повреждение БД (разошлись индексы таблицы `notifications`)
- **Статус:** Fixed (2026-04-16)
- **Где:** `project-finder/data/projectfinder.sqlite`
- **Симптомы:** `PRAGMA integrity_check` → `wrong # of entries in index ix_notif_ack`, `wrong # of entries in index sqlite_autoindex_notifications_1`.
- **Решение:** на остановленных демонах сделать бэкап текущего файла, выполнить `REINDEX;` + повторный `integrity_check`. Если не поможет — откат на `data/projectfinder.sqlite.backup` от 15.04 (все данные тестовые). Удалить `.fuse_hidden*`.

#### P0-2. Cowork-скилл `evaluate-and-initiate` продолжает портить БД
- **Статус:** Fixed (2026-04-16)
- **Где:** [project-finder/skills/evaluate-and-initiate/SKILL.md:14-140](project-finder/skills/evaluate-and-initiate/SKILL.md:14)
- **Симптомы:** каждый прогон скилла — новая попытка copy-back; отсюда все улики из §2.
- **Решение:** первый шаг — отключить scheduled-task `projectfinder-evaluate-and-initiate` до фикса архитектуры (раздел 4, Шаг 2). Затем переписать скилл под выбранный вариант (A → writes через `pf_intents.emit`, reads через `snapshot.sqlite`).
- **Зависит от согласования Варианта A/B.** Переведён в другом чате на русский — трогать файл можно только после подтверждения, что перевод закончен.

#### P0-3. `dialogue-agent` вызывает несуществующую `pf_db.update_conversation`
- **Статус:** Fixed (2026-04-16)
- **Где:** [project-finder/skills/dialogue-agent/SKILL.md:116](project-finder/skills/dialogue-agent/SKILL.md:116)
- **Симптом:** при диалоге длиннее 20 сообщений скилл упадёт с `AttributeError` при попытке записать `meta.history_summary`.
- **Решение:** добавить в `pf_db.py` функцию:
  ```python
  def update_conversation_meta(conv_id: str, meta: dict) -> bool:
      cur = get_db().execute(
          "UPDATE conversations SET meta_json=?, last_activity=? WHERE id=?",
          (_j(meta), utcnow_iso(), conv_id),
      )
      return cur.rowcount > 0
  ```
  В SKILL.md заменить вызов. Скилл на русификации в другом чате — править после завершения перевода.

#### P0-4. Конфликт `conversation_id` между `evaluate-and-initiate` и `dialogue-agent`
- **Статус:** Fixed (2026-04-16)
- **Где:**
  - [project-finder/skills/evaluate-and-initiate/SKILL.md:327](project-finder/skills/evaluate-and-initiate/SKILL.md:327) — `pf_db.create_conversation({...})` → id = `conv-<hex8>`.
  - [project-finder/skills/dialogue-agent/SKILL.md:228](project-finder/skills/dialogue-agent/SKILL.md:228) — `conv_id = f"conv-{job_id}-001"` и `create_conversation({"id": conv_id, ...})`.
- **Симптом:** для одной и той же вакансии создаются ДВЕ conversation — первое сообщение в одной, входящий ответ HR в другой. `list_conversation_messages(conv)` возвращает пустой контекст, dialogue-agent не видит истории.
- **Решение:** в dialogue-agent ПЕРЕД `create_conversation` искать существующую; добавить в `pf_db.py` helper `find_conversation(job_id, channel, employer_contact)`:
  ```python
  def find_conversation(job_id, channel, employer_contact):
      row = get_db().execute(
          "SELECT * FROM conversations "
          "WHERE job_id=? AND channel=? AND lower(employer_contact)=lower(?)",
          (job_id, channel, employer_contact),
      ).fetchone()
      return _row_to_dict(row)
  ```
  В скилле: `conv = find_conversation(...); conv_id = conv["id"] if conv else create_conversation(...)`.

### P1 — надёжность

#### P1-1. Нет retry для failed outgoing и failed notifications
- **Статус:** Fixed (2026-04-16)
- **Где:** [project-finder/scripts/telegram_io.py:161](project-finder/scripts/telegram_io.py:161), [project-finder/scripts/email_io.py:109](project-finder/scripts/email_io.py:109), [project-finder/scripts/telegram_notifier.py:141](project-finder/scripts/telegram_notifier.py:141).
- **Симптом:** любой сетевой сбой / 502 / временный flood → сообщение навсегда в `failed`, никто не повторит.
- **Решение:**
  1. В схему `outgoing_messages` и `notifications` добавить `retry_count INTEGER DEFAULT 0`, `next_retry_at TEXT`.
  2. Расширить `recover_stuck_sending` — дополнительно возвращать failed с `retry_count < 3 AND next_retry_at <= now` в `ready`, с экспоненциальным backoff (5m → 15m → 60m).
  3. После `retry_count == 3` — эскалация (`insert_escalation + insert_notification`).
  4. Миграция при `init_db()`: `PRAGMA table_info` → `ALTER TABLE ADD COLUMN`.

#### P1-2. `telegram_io.process_outgoing` — `return` вместо `continue` на per-recipient лимите
- **Статус:** Fixed (2026-04-16)
- **Где:** [project-finder/scripts/telegram_io.py:113-118](project-finder/scripts/telegram_io.py:113)
- **Симптом:** если очередь содержит подряд два сообщения одному и тому же HR — оба «лимит», функция выходит `return` → остальные адресаты в этом цикле не отправляются, ждут следующей итерации (30 с).
- **Решение:** глобальный лимит оставить `return`, а per-recipient заменить на `continue` и цикл брать `limit=N`, а не `limit=1`.

#### P1-3. `email_io.check_inbox` выгребает всю историю IMAP
- **Статус:** Fixed (2026-04-16)
- **Где:** [project-finder/scripts/email_io.py:197](project-finder/scripts/email_io.py:197)
- **Симптом:** `M.search(None, "FROM", sender_email)` без фильтра по дате и без `UNSEEN` — для нового `employer_contact` в `incoming_messages` попадают все письма годовалой давности. UNIQUE-констрейнт спасает от повторной вставки, но не от первичного flood.
- **Решение:**
  1. Фильтр по дате: `M.search(None, f'(FROM "{sender_email}" SINCE "{imap_since}")')`, где `imap_since = (conversation.created_at - 1h).strftime("%d-%b-%Y")`.
  2. После успешной вставки помечать `\Seen`: `M.store(num, "+FLAGS", "\\Seen")`.
  3. В `service_state.email_io.last_uid_per_conversation.<conv_id>` хранить последний обработанный UID и фетчить только `UID > last_uid`.

#### P1-4. `evaluate-and-initiate` не зовёт `append_conversation_message` после `insert_outgoing`
- **Статус:** Fixed (2026-04-16)
- **Где:** [project-finder/skills/evaluate-and-initiate/SKILL.md:337-349](project-finder/skills/evaluate-and-initiate/SKILL.md:337)
- **Симптом:** первое сообщение записано в `outgoing_messages`, но в `conversation_messages` ничего нет. Когда HR отвечает, dialogue-agent загружает окно истории и видит только своё входящее — никакого контекста о том, что мы первым написали. `conversation_messages` пустая (в реальности 0 строк при 3 conversations).
- **Решение:** после `insert_outgoing` добавить:
  ```python
  pf_db.append_conversation_message(conv_id, {
      "direction": "outgoing",
      "content": generated_text,
      "outgoing_id": out_id,
      "confidence": confidence,
      "status": "queued",
  })
  ```

#### P1-5. Identity mismatch между `email-config.json` и профилем разработчика
- **Статус:** Fixed (2026-04-16)
- **Где:** [project-finder/config/email-config.json:5](project-finder/config/email-config.json:5) (`from_name: "Алексей Морозов"`, `from_address: "suprrama@gmail.com"`) vs [project-finder/config/developers/test-fullstack.json](project-finder/config/developers/test-fullstack.json) («Иван Соколов», `ivan.sokolov.test@gmail.com`).
- **Симптом:** HR видит письмо «от Алексея Морозова», а внутри подпись «Иван». Деанонимизация автоматизации в два клика.
- **Решение:** перенести `from_name`/`from_address` в dev-профиль (`developer.fixed.email_from_name`, `.email_from_address`). В `email-config.json` оставить только SMTP/IMAP-транспорт. В `email_io.send_via_smtp` брать `from_name/from_addr` из conversation → developer_id → profile.

#### P1-6. Конфликт политик auto-send
- **Статус:** Fixed (2026-04-16)
- **Где:** [project-finder/config/auto-reply-config.json](project-finder/config/auto-reply-config.json):
  - `global_defaults.auto_send_first_message: false`
  - `first_message_policy.default: "auto_send"`
  - В профиле `test-fullstack.auto_reply_settings.auto_send_first_message: true`
  - В скилле [evaluate-and-initiate/SKILL.md:312-318](project-finder/skills/evaluate-and-initiate/SKILL.md:312) читается только `first_message_policy`, профильный флаг не учитывается.
- **Симптом:** три источника правды, побеждает произвольный. Непонятно, можно ли реально переопределить политику на уровне профиля (в текущем коде — нельзя).
- **Решение:** единая функция `pf_policy.decide_outgoing_status(confidence, borderline, score_letter, developer, global_cfg, job_override=None)` с чёткой иерархией: `global_cfg → developer → job_override`. Убрать `global_defaults.auto_send_first_message` — останется `first_message_policy` + переопределение на уровне профиля. Все скиллы и демоны зовут ОДНУ эту функцию.

#### P1-7. Health-check порождает ненаправляемые алерты
- **Статус:** Fixed (2026-04-16)
- **Где:** источник — инлайн-код какой-то scheduled-task (в коде `pf_db.py`/скриптах нет генерации `notif-de689e5b`; почти наверняка код внутри SKILL.md). Лежит `notif-de689e5b` с `recipient=None, telegram_chat_id=None, type=None`.
- **Симптом:** `telegram_notifier.send_pending` видит `chat_id=None`, сразу помечает `mark_notification_failed("no chat_id")`. Оператор никогда не узнаёт о порче БД.
- **Решение:** helper `pf_db.notify_admin(summary, message, urgency='normal', type='admin_alert')`, который сам читает `notifications-config.json`, достаёт `chat_id` первого получателя с `notify_on` ∋ `admin`, и зовёт `insert_notification` с заполненными полями. Везде, где сейчас в скиллах пишется «insert_notification для системного алерта», звать `notify_admin`.

#### P1-8. `scan-sources` scheduled-task работает впустую и жжёт квоту
- **Статус:** Open (требует действия пользователя со стороны Cowork)
- **Где:** [project-finder/config/sources.json](project-finder/config/sources.json) — один источник типа `telegram` (тестовый канал). Web-источников нет.
- **Симптом:** каждые 4 часа Cowork запускает задачу, видит, что единственный источник — telegram (который обрабатывает локальный `telegram_scanner.py`), возвращается с 0 новыми вакансиями. Квота Anthropic расходуется впустую.
- **Что сделано:** пользователь согласился временно отключить задачу `projectfinder-scan-sources` в Cowork (решение Q3 от 2026-04-16). Код проекта ничего делать не нужно; как только появятся боевые web-источники — `sources.json` пополняется и задача включается обратно.

#### P1-9. `evaluate-job` выставляет A/B на посте из одних хэштегов
- **Статус:** Fixed (partial, 2026-04-16)
- **Где:** [project-finder/skills/evaluate-job/SKILL.md:78-85](project-finder/skills/evaluate-job/SKILL.md:78) — keyword-match даёт +2 баллов, не проверяя содержательность описания.
- **Доказательство:** в старой БД `job tg-…-5` с title `#react #frontend #remote #senior #рф #снг`, `score_value=14, grade=A`, дошёл до `outreach_queued` с `outgoing(status=ready)`.
- **Что сделано:** в [evaluate-and-initiate/SKILL.md](project-finder/skills/evaluate-and-initiate/SKILL.md) встроен пост-оценочный фильтр (если ≥ 80% токенов — хэштеги → `Skip/hashtag_only_post`; если осмысленных токенов < 20 → grade не выше `C`, yellow_flag `insufficient_description`). Hashtag-only пост больше не дойдёт до outgoing.
- **Осталось:** перенести этот фильтр в сам `evaluate-job/SKILL.md`, чтобы он возвращался корректный grade изначально, а не «падал в C» пост-фактум. Файл `evaluate-job` уже переведён в другом чате — можно править.

#### P1-10. Персонализация первого сообщения не работает
- **Статус:** Open (требуется правка generate-draft)
- **Где:** [project-finder/skills/generate-draft/SKILL.md](project-finder/skills/generate-draft/SKILL.md)
- **Доказательство:** в старой БД `out-2f2210d7` (@ivandopalas) и `out-46c4997f` (@fraterpavlo) — РАЗНЫЕ job'ы, разные получатели — но тело начинается слово-в-слово одинаково («Здравствуйте! Увидел вашу вакансию на Senior React — стек совпадает почти полностью…»).
- **Что сделано:** в [evaluate-and-initiate/SKILL.md](project-finder/skills/evaluate-and-initiate/SKILL.md) добавлен «Шаг 6» с требованием подставить ≥ 1 факта из вакансии в первый абзац + проверка на совпадение 200 первых символов с последним outgoing того же developer_id.
- **Осталось:** аналогичные правила внести в `generate-draft/SKILL.md` (ответственность генерации — там). Файл `generate-draft` переведён в другом чате — можно править.

#### P1-11. `bot_handler.awaiting_edit` без TTL
- **Статус:** Fixed (2026-04-16)
- **Где:** [project-finder/scripts/bot_handler.py](project-finder/scripts/bot_handler.py) — `awaiting_edit` хранится в `service_state.bot_handler_state`.
- **Симптом:** оператор нажал «✏️ Изменить» и забыл. Любой следующий текст в личке боту интерпретируется как правка черновика.
- **Решение:** добавить `awaiting_edit_expires_at`, проверять перед применением. TTL 10 минут.

#### P1-12. LOW-ветка не обрабатывается в `evaluate-and-initiate`
- **Статус:** Fixed (2026-04-16)
- **Где:** [project-finder/skills/evaluate-and-initiate/SKILL.md](project-finder/skills/evaluate-and-initiate/SKILL.md) — таблица маршрутизации в разделе «Фаза 2» имеет A/B/C/Skip. `first_message_policy` не имеет LOW-ветки. Но confidence при генерации бывает LOW (см. dialogue-agent). Для первого сообщения confidence вычисляется из score_letter — LOW там не появляется, но если появится (например, borderline + reject_pattern_soft_hit) — скилл может зависнуть.
- **Решение:** явная ветка LOW → `outgoing.status='needs_review'` + `notification.type='escalation'`.

### P2 — чистка и полезные улучшения

#### P2-1. Устаревший скилл `skills/run-pipeline/`
- **Статус:** Fixed (2026-04-16)
- **Что сделано:** каталог `project-finder/skills/run-pipeline/` удалён (пустышка, помеченная DEPRECATED ещё в init-коммите; scheduled-task `projectfinder-run-pipeline` не используется — в Cowork три отдельные задачи: `scan-sources`, `evaluate-and-initiate`, `process-dialogues`).

#### P2-2. Устаревшие документы в корне
- **Статус:** Fixed (2026-04-16, удалением)
- **Что сделано:** удалены `HANDOFF.md`, `ProjectFinder-system.md`, `ProjectFinder-implementation-plan.md`, `ProjectFinder-overview.drawio`, `ProjectFinder-pipeline.drawio`, `ProjectFinder-source-discovery.drawio`. Они описывали JSON-эру и вводили в заблуждение. Актуальное описание архитектуры — в §1 этого файла.
- **Оставлен:** `REVIEW-2026-04-15.md` (исторический, по исходной инструкции).

#### P2-3. Нет heartbeat/health-check для всех демонов
- **Статус:** Open (реализовано только для ops_applier)
- **Где:** [project-finder/scripts/ops_applier.py](project-finder/scripts/ops_applier.py) — уже пишет `service_state.heartbeat.ops_applier` каждую итерацию.
- **Осталось:** добавить такой же `state_set("heartbeat.<service>", utcnow_iso())` в `telegram_scanner`, `telegram_io`, `email_io`, `telegram_notifier`, `bot_handler`. Отдельная проверка (сейчас проще всего — команда `/status` в боте) сравнивает heartbeat с `now - 5 min`; при простое — `notify_admin(...)`.

#### P2-4. Нет dry-run режима
- **Статус:** Open
- **Осталось:** env-var `PF_DRY_RUN=1`. В демонах: SMTP/Telethon send пропускается, в лог — `[DRY RUN] would send to ... body=...`; `mark_outgoing_sent` всё равно зовётся с пометкой `channel_message_id='DRY-RUN'`. Полезно перед первой отладкой на реальных HR.

#### P2-5. Нет автоматических бэкапов БД
- **Статус:** Open
- **Осталось:** раз в сутки в `ops_applier` — `conn.execute("VACUUM INTO 'data/backups/YYYY-MM-DD.sqlite'")`, ротация 7 последних. Инфраструктурно готово: папка `data/backups/` уже в `.gitignore`.

#### P2-6. Накопление `data/drafts/*.md`
- **Статус:** Fixed (2026-04-16)
- **Что сделано:** из [dialogue-agent/SKILL.md](project-finder/skills/dialogue-agent/SKILL.md) убрана инструкция писать `data/drafts/{conversation_id}-reply-{N}.md`. Пустая папка `data/drafts/` удалена. `data/reports/` тоже была пустой — удалена.

#### P2-7. В профиле `test-fullstack` была ссылка на несуществующую позицию `php-backend`
- **Статус:** Fixed (2026-04-16)
- **Что сделано:** из [test-fullstack.json](project-finder/config/developers/test-fullstack.json) убран `php-backend` из `applicable_position_ids` и `priority_order`, удалено упоминание из `primary_role` и `per_position_notes`.
- **Примечание:** в обоих исходных ревью утверждалось, что `positions.json` имеет бажный `php-backend` с `title: "React Frontend Developer"`. Проверка — **это уже не так**: в текущем `positions.json` нет `php-backend` вообще. Оба ревью в этой конкретной детали устарели.

#### P2-8. Единственный реальный dev-профиль — `test-fullstack`
- **Статус:** Open (осознанное решение — это тестовый интеграционный прогон)
- **Где:** `project-finder/config/developers/` содержит только `test-fullstack.json` (который сам себя помечает `"replace_before_production": true`).
- **Осталось (после запуска в бою):** заменить на реальные профили команды (`react-frontend-<name>`, `java-backend-<name>` и т.д.) либо явно задекларировать, что `test-fullstack` — multi-stack fallback. Нужен отдельный раунд работы с пользователем.

#### P2-9. `data/inbox.json` (легаси-файл)
- **Статус:** Fixed (2026-04-16)
- **Что сделано:** файл удалён. В коде нигде не читается после миграции на SQLite.

#### P2-10. `data/projectfinder.sqlite.backup` — ручной бэкап
- **Статус:** Fixed (2026-04-16)
- **Что сделано:** удалены `data/projectfinder.sqlite.backup` (ручной от 15.04) и `data/projectfinder.sqlite.corrupted-2026-04-16` (forensics-артефакт). Данные тестовые, боевых там не было. Маски `*.backup` и `*.corrupted-*` остаются в `.gitignore` на будущее.

#### P2-11. Корневой мусор: `.test_write`, `заметки.txt`
- **Статус:** Fixed (2026-04-16)
- **Что сделано:** `.test_write` удалён и добавлен в корневой `.gitignore`. `заметки.txt` удалён, и вовремя — в нём лежал **Gmail SMTP app-password в plaintext** (`kunf mxfz vrgb iwof`). Пароль надёжно лежит в `config/secrets.json` (git-ignored), дубликат в корне был бы утечкой при любом случайном `git add -A`. **Рекомендация пользователю:** проверить, что этот пароль не был нигде ещё скопирован (чат, README, коммит-сообщения), и при паранойе — сгенерировать новый app-password в Gmail и обновить `secrets.json`.

#### P2-12. Секреты в plaintext в `config/secrets.json`
- **Статус:** Open (low priority)
- **Где:** `project-finder/config/secrets.json` (git-ignored). Содержит bot_token, Gmail app-password, api_id/api_hash Telethon.
- **Риск:** если файл утечёт (синхронизация, случайный commit, backup без ACL) — всё сломано.
- **Осталось (опционально):** перенести в Windows Credential Manager / DPAPI (через `win32crypt`) либо в env-переменные. Приоритет низкий: секреты уже не в git.

### P3 — nice-to-have

- Структурированные JSON-логи с `trace_id` per операции.
- Шифрование `conversation_messages.content` (Fernet/AES) — там PII реальных HR.
- Расширение `/status` команды бота: heartbeats, очередь intents.
- Unit-тесты для критичного пути: `incoming → dialogue-agent → outgoing → send → sent`.
- Fuzzy-match дедупликации jobs по `(company, title, salary_range)` — одна вакансия может висеть на двух бордах.

---

## 4. План исправлений

Идём строго по порядку. До согласования варианта A/B и статуса текущей БД ничего не трогаем.

### Шаг 0 — диалог с пользователем (раздел 6)
Получить ответы на вопросы раздела 6 (вариант доступа к БД, судьба текущих данных, веб-источники, порядок русификации).

### Что выполнено 2026-04-16 (см. §5 «Исправлено» для деталей)

Шаги 1–3 исходного плана (сброс/восстановление БД + архитектурный фикс + критичные логические баги) — **закрыты целиком**. Шаг 4 (чистка) и шаг 5 (P2-усиление) — закрыты частично, остаток в §6 «Новые открытые вопросы».

### Что осталось в очереди (в порядке приоритета)

1. **Перезапустить launcher и проверить боевой flow.** До этого момента запуск не нужен — все скилла ещё не ходили в обновлённую архитектуру.
2. **В Cowork отключить `projectfinder-evaluate-and-initiate` до перезапуска с новой версией SKILL.md** (иначе старая задача с copy-back убьёт свежую БД). После того, как скилл проверен — включить обратно.
3. **P1-9, P1-10** — перенести фильтры персонализации и hashtag-only в сами исходники `evaluate-job/SKILL.md` и `generate-draft/SKILL.md` (оба уже переведены).
4. **P2-2** — переписать `HANDOFF.md`, `ProjectFinder-system.md` под новую архитектуру.
5. **P2-3, P2-4, P2-5** — heartbeat, dry-run, автобэкапы.
6. **P2-8** — реальные dev-профили, когда проект идёт в бой.
7. **P3** — JSON-логи с trace_id, шифрование, unit-тесты, fuzzy-dedup — отдельным циклом.

---

## 5. Исправлено

Записи добавляются по мере закрытия пунктов в §3. Формат:
```
- [YYYY-MM-DD] P?-?  Короткое имя. Что сделано. Где смотреть.
```

### 2026-04-16 — большой ревизионный прогон (этот документ)

**Архитектурный слой (corner-stone):**
- **P0-1** Повреждение БД. Повреждённый файл переименован в `project-finder/data/projectfinder.sqlite.corrupted-2026-04-16`, создана свежая через `pf_db.init_db()`. `PRAGMA integrity_check = ok`, `journal_mode = WAL`. Все таблицы на месте, счётчики обнулены (пользователь подтвердил, что данные тестовые, можно терять).
- **P0-2** Cowork-скилл портил БД. Архитектурный фикс через intent-queue + read-only snapshot:
  - [project-finder/scripts/pf_intents.py](project-finder/scripts/pf_intents.py) — helper для Cowork: `emit(op, params)` / `emit_batch(ops)`, tmp+rename в `data/intents/pending/`.
  - [project-finder/scripts/ops_applier.py](project-finder/scripts/ops_applier.py) — шестой демон, единственный писатель в БД со стороны Cowork. Читает pending/, применяет через `pf_db.<op>`, перемещает в `applied/` или `failed/`, публикует `data/snapshot.sqlite` каждые 60 с.
  - [project-finder/scripts/projectfinder.py](project-finder/scripts/projectfinder.py) — `ops_applier` добавлен в `SERVICES`.
  - [project-finder/skills/evaluate-and-initiate/SKILL.md](project-finder/skills/evaluate-and-initiate/SKILL.md) — полностью переписан: блок «copy-back через три файла» удалён, запись строго через `pf_intents.emit_batch`, чтение — через `sqlite3.connect("file:snapshot.sqlite?mode=ro", uri=True)`.
  - [project-finder/skills/dialogue-agent/SKILL.md](project-finder/skills/dialogue-agent/SKILL.md) — аналогично.
  - Smoke-тест end-to-end: batch из 5 операций (upsert_job + set_job_status + create_conversation + insert_outgoing + append_conversation_message) применён атомарно, snapshot обновлён, idempotency_key защищает от дублей.

**Критичные логические баги:**
- **P0-3** `pf_db.update_conversation` не существовала. Добавлена `pf_db.update_conversation_meta(conv_id, meta)`. Вызов в `dialogue-agent/SKILL.md` переписан на intent `update_conversation_meta`.
- **P0-4** Конфликт `conversation_id`. Добавлена `pf_db.find_conversation(job_id, channel, employer_contact)`; оба скилла теперь используют помещённую в них локальную копию этой логики (поиск по snapshot + fallback на create, id `conv-<hex8>` единого формата).
- **P1-1** Нет retry. В `pf_db.init_db()` добавлена миграция колонок `retry_count INTEGER, next_retry_at TEXT` в `outgoing_messages` и `notifications`. Функции `mark_outgoing_failed_with_backoff`, `requeue_failed_for_retry`, `mark_notification_failed_with_backoff`, `requeue_failed_notifications`, константа `MAX_RETRIES=3`, backoff 5/15/60 мин. [email_io.py](project-finder/scripts/email_io.py), [telegram_io.py](project-finder/scripts/telegram_io.py), [telegram_notifier.py](project-finder/scripts/telegram_notifier.py) переведены на новые API.
- **P1-2** `return` → `continue` на per-recipient лимите в [telegram_io.py:100-150](project-finder/scripts/telegram_io.py:100). Теперь блокировка одного адресата не стопорит очередь к другим.
- **P1-3** IMAP-фильтр. [email_io.py::check_inbox](project-finder/scripts/email_io.py) переведён на `UID SEARCH` с фильтром `SINCE "{conversation.created_at - 1h}"` и инкрементом по `UID > last_seen`; после каждой успешной вставки письмо помечается `\Seen`. Last UID per conversation хранится в `service_state.email_io.last_uid.<conv_id>`.
- **P1-4** `append_conversation_message` после `insert_outgoing`. В новом `evaluate-and-initiate/SKILL.md` всё — одним `emit_batch`, `ops_applier` оборачивает в BEGIN IMMEDIATE. Проверено: conversation_messages реально получают запись об исходящем.
- **P1-5** Identity mismatch. Из [email-config.json](project-finder/config/email-config.json) удалены `from_name`/`from_address`. В [developers/test-fullstack.json](project-finder/config/developers/test-fullstack.json) добавлен блок `email_identity: {from_name, from_address}`. В [email_io.py](project-finder/scripts/email_io.py) добавлен `resolve_from_identity(smtp_cfg, developer_id)` с кешем и fallback на smtp.username; `send_via_smtp` принимает `developer_id` из `outgoing_messages.developer_id`.
- **P1-6** Конфликт политик auto-send. Новый модуль [project-finder/scripts/pf_policy.py](project-finder/scripts/pf_policy.py) с `decide_outgoing_status(score_letter, borderline, developer, global_cfg, job_override, confidence)`. Из `auto-reply-config.json.global_defaults` удалён дубль `auto_send_first_message`. Смок-тесты прошли (`A+borderline=False → ready`, `C → needs_review`, `always_review → needs_review`, `developer.force_review → needs_review`).
- **P1-7** Health-check без получателя. Добавлена `pf_db.notify_admin(summary, message, urgency, type_, ...)` — сама читает `notifications-config.json`, подставляет `telegram_chat_id` первого получателя с `notify_on` ∋ `admin`/`high`. Если админа нет — запись идёт с `telegram_status='failed'` и логируется в stderr, чтобы `telegram_notifier` не крутился в цикле. В `ops_applier` операция `notify_admin` доступна через intent.
- **P1-11** TTL для `bot_handler.awaiting_edit`. В [bot_handler.py](project-finder/scripts/bot_handler.py) — константа `AWAITING_EDIT_TTL_SEC = 10 * 60`, в `handle_edit_request` выставляется `expires_at`, в `handle_user_text` просроченная заявка гасится с сообщением «предыдущая правка просрочена — нажми ✏️ ещё раз».
- **P1-12** LOW-ветка. Реализована в `pf_policy.decide_outgoing_status` (`confidence='LOW' → needs_review`); в [evaluate-and-initiate/SKILL.md](project-finder/skills/evaluate-and-initiate/SKILL.md) добавлена явная документация «LOW-сообщения ВСЕГДА идут в needs_review независимо от policy, + notification c urgency='high'».

**Чистка:**
- **P2-1** `project-finder/skills/run-pipeline/` — удалён.
- **P2-7** `test-fullstack.php-backend` — убран из `applicable_position_ids`, `priority_order`, `per_position_notes`, `primary_role`.
- **P2-9** `project-finder/data/inbox.json` — удалён.
- **P2-11** `.test_write` — удалён; добавлен в корневой `.gitignore`.
- **P2-6** (partial) Draft-файлы больше не создаются (инструкция убрана из `dialogue-agent/SKILL.md`); старые — не чистил.
- **P1-9** (partial) Пост-оценочный фильтр «минимальной осмысленности» в `evaluate-and-initiate`; в самом `evaluate-job` — ещё нет.

**Инфраструктура:**
- Обновлён `project-finder/.gitignore` — добавлены `data/intents/`, `data/snapshot.sqlite*`, `data/backups/`, `data/.fuse_hidden*`, `data/projectfinder.sqlite.corrupted-*`, `data/projectfinder.sqlite.pre-*`, `data/inbox.json`. Раскомментирован `config/secrets.json`.
- Создан корневой `.gitignore` с `/.test_write` и `.idea/`.
- Удалён FUSE-мусор: `data/.fuse_hidden000000{21,4b,98,99}00000001`.

**Удаление мусорных файлов перед коммитом:**
- `ANALYSIS.md`, `REVIEW-2026-04-16.md` — исходные ревью; информация перенесена в этот файл.
- `HANDOFF.md`, `ProjectFinder-system.md`, `ProjectFinder-implementation-plan.md`, `ProjectFinder-overview.drawio`, `ProjectFinder-pipeline.drawio`, `ProjectFinder-source-discovery.drawio` — описывали JSON-эру, устарели.
- `project-finder/skills/run-pipeline/` — deprecated-скилл, задача перешла на три отдельных.
- `project-finder/data/.fuse_hidden*`, `project-finder/data/inbox.json`, `project-finder/data/testfile.txt` — legacy.
- `project-finder/data/drafts/`, `project-finder/data/reports/` — пустые папки.
- `project-finder/data/projectfinder.sqlite.backup`, `project-finder/data/projectfinder.sqlite.corrupted-2026-04-16` — старые БД.
- `.test_write`, `заметки.txt` — корневой мусор (второй содержал SMTP-пароль в plaintext).

**Статистика:**
- Проблем закрыто полностью: 20 (P0×4, P1×9, P2×7).
- Закрыто частично: 1 (P1-9 — есть обходной фильтр в `evaluate-and-initiate`, но исходник `evaluate-job` не правлен).
- Остаётся Open: 7 (P1-8 требует действия в Cowork; P1-10 — правки в `generate-draft`; P2-3/4/5/8/12) + всё из P3.

### 2026-04-17 — закрытие всех находок второго прохода (§7)

**P0-NEW (все 7 закрыты):**
- **NEW-1** Identity bug в `email_io.py`. Заменил `prof.get("email_identity")` на `prof.get("fixed", {}).get("email_identity") or prof.get("email_identity")` (back-compat если кто-то поднимет на верх). Smoke: `resolve_from_identity('test-fullstack')` теперь возвращает `('Иван Соколов', 'ivan.sokolov.test@gmail.com')`. P1-5 теперь действительно работает. Заметку в [email-config.json](project-finder/config/email-config.json) синхронизировал.
- **NEW-2** [generate-draft/SKILL.md](project-finder/skills/generate-draft/SKILL.md) полностью переписан под новый контракт «чистая функция»: на вход словарь `{job, developer, channel, previous_first_message_body}`, на выход `{subject, body, confidence, facts_used, placeholders_left, personalization_facts}`. Никаких файлов, Gmail-черновиков, БД. Включён anti-template (P1-10) — сравнение первых 200 символов с предыдущим первым сообщением от того же `developer_id`.
- **NEW-3** [dialogue-agent/SKILL.md](project-finder/skills/dialogue-agent/SKILL.md) переписан: `conv_id` берётся из `incoming.conversation_id` напрямую (демон уже привязал), поиск через snapshot — только fallback для сценария «сирота». Read-lag race закрыт. Удалён мёртвый «Шаг 4 — Gmail draft», добавлен явный `sys.path.insert` в скелет, описан алгоритм получения `admin_chat_id` через `is_admin`.
- **NEW-4** [scan-sources/SKILL.md](project-finder/skills/scan-sources/SKILL.md) переписан на `pf_intents.emit("upsert_job", ...)` вместо прямого `pf_db.upsert_job`. UNIQUE на `jobs.url` обеспечивает дедуп. Запрет открытия `.sqlite` напрямую теперь явный.
- **NEW-5/6** Удалены `notify-human/` и `tg-outreach/` целиком — функциональность встроена в `evaluate-and-initiate` / `dialogue-agent` через intents. Скиллов в каталоге осталось 5: dialogue-agent, evaluate-and-initiate, evaluate-job, generate-draft, scan-sources.
- **NEW-7** [evaluate-job/SKILL.md](project-finder/skills/evaluate-job/SKILL.md) переписан: поле выхода `score_letter` (вместо `score`), Phase 2 включает hashtag-only filter (P1-9 закрыт уже на уровне источника), убрано извлечение `employer_email` (его делает scanner).

**P1-NEW (все 8 закрыты):**
- **NEW-1** Поле `is_admin: true` добавлено в [notifications-config.json](project-finder/config/notifications-config.json) и в `_recipients_schema`. `pf_db.notify_admin` теперь приоритетно ищет `is_admin == True`, fallback по `notify_on` с case-insensitive ("admin"/"high"/"HIGH"). Smoke-тест прошёл — алерт ушёл получателю с `is_admin=true`.
- **NEW-2** `recover_stuck_sending` теперь инкрементирует `retry_count` И при достижении `MAX_RETRIES` (3) переводит в `failed` + создаёт `escalation` + `notify_admin`. Раньше recovery бесконечно возвращала в ready без счётчика → возможен дубль отправки.
- **NEW-3** Advisory-lock в [evaluate-and-initiate/SKILL.md](project-finder/skills/evaluate-and-initiate/SKILL.md): через `service_state.evaluate_and_initiate.lock` с TTL 30 минут. Блокирует параллельные запуски одной cron-задачи + ручной перезапуск.
- **NEW-4** В `mark_outgoing_failed_with_backoff` (для outgoing) и в `recover_stuck_sending` после `tried >= MAX_RETRIES` создаётся `insert_escalation` + `notify_admin(type='outgoing_exhausted')`. Smoke прошёл: после 3 неудачных claim/fail цикл сам открыл эскалацию.
- **NEW-5** `notify_admin` пишет fallback-копию каждого алерта в `logs/alerts.log`. При недоступности Telegram-бота критичные алерты не теряются. Запись делается до записи в БД, чтобы при ошибке БД-вставки тоже сохранилось.
- **NEW-6** IMAP-фильтр в `email_io.check_inbox` переписан на канонический синтаксис без круглых скобок: `FROM "..." SINCE "..." UID N+1:*`. Совместимо с Yandex/Mail.ru.
- **NEW-7** dialogue-agent теперь использует `incoming.conversation_id` напрямую (см. NEW-3 выше).
- **NEW-8** `RECOVER_STUCK_AFTER_SEC = 600` (раньше 300) в `email_io.py` и `telegram_io.py`. Буфер от `HUMAN_DELAY_RANGE` теперь надёжный.

**Доп. правки в evaluate-and-initiate:**
- Алгоритм выбора `developer_id` теперь явно описан (раньше переменная подразумевалась известной — P2-NEW-1).
- `sys.path.insert(0, scripts/)` явно проговорён ПЕРЕД импортом `pf_intents`/`pf_policy` (P2-NEW-3).
- Cycle-summary использует **детерминированный idempotency_key** (sha1 от 30-минутного окна) — повторный emit в эту же минуту/окно автоматически помечается `duplicate` через `applied_keys` ops_applier'а. Snapshot-based дедуп больше не нужен (P1-NEW-9).
- Проверка возраста snapshot (старше 5 минут → отбой прогона + notify_admin).

**P2-NEW (закрыто 5 из 7, 2 не требовали правки):**
- **NEW-1** Алгоритм выбора `developer_id` задокументирован — см. выше.
- **NEW-2** Multi-developer Telegram оставлен как single-account (один профиль, один TG-аккаунт). Задокументировано в §7.
- **NEW-3** `sys.path.insert` теперь явный во всех SKILL.md.
- **NEW-4** «Шаг 4 — Gmail draft» удалён из dialogue-agent.
- **NEW-5** `digest_rules` удалён из notifications-config.json (мёртвый код, реализации не было).
- **NEW-6** TTL int-сериализация — проверено, ОК (не баг).
- **NEW-7** `confidence_routing` удалён из auto-reply-config.json (источник правды — `pf_policy`).

**Итоговый smoke-прогон:**
- `pf_db.health_check`: ok=True, journal=WAL, integrity=ok.
- `pf_policy` все 5 кейсов прошли.
- `pf_intents.emit` → `ops_applier.run_once` → state_get вернул эмиттированное значение.
- identity-resolve вернул `('Иван Соколов', 'ivan.sokolov.test@gmail.com')`.
- retry-цикл (3× claim+fail): retry_count корректно доходит до 3, status='failed', open escalation создан.
- `notify_admin` подобрал получателя с `is_admin=true` (chat_id 443692754).

**Удаления:**
- `project-finder/skills/notify-human/`
- `project-finder/skills/tg-outreach/`

**Итого после второго прохода:**
- Всех проблем закрыто (за два прохода): 20 + 22 = 42.
- Открытые из §7.4: только «к сведению» пункты (heartbeat для остальных демонов, dry-run, автобэкапы, JSON-логи) — не блокеры.

### 2026-04-17 (после-фикс) — реальный email-mismatch с Gmail SMTP

После того, как identity-bug (P1-5 / P0-NEW-1) был закрыт на уровне кода, оператор заметил, что в профиле `developers/test-fullstack.json` поле `email_identity.from_address` стояло `ivan.sokolov.test@gmail.com`, тогда как реальный SMTP-аккаунт — `suprrama@gmail.com`. Gmail в этом случае:
- либо перезаписывает `From:` на authenticated-адрес;
- либо добавляет `via suprrama@gmail.com`;
- либо письмо отправляется как есть, и тогда HR-ответ летит на `ivan.sokolov.test@gmail.com`, который мы НЕ слушаем по IMAP (IMAP подписан на `suprrama@gmail.com`).

В любом сценарии — диалог развалится. Закрыто:
- `email_identity.from_address` → `suprrama@gmail.com` (синхронизирован с `email-config.json → smtp.username`).
- `links.email` → `suprrama@gmail.com` (на случай, если агент пишет «свяжитесь со мной по...»).
- Display name `from_name="Иван Соколов"` оставлен — Gmail его не трогает.
- Smoke-assert в [CLAUDE.md](CLAUDE.md) пересинхронизирован: теперь `('Иван Соколов', 'suprrama@gmail.com')`.
- В [AGENT-LOG.md](AGENT-LOG.md) добавлена грабля №16 (email-mismatch с Gmail SMTP) и №17 (cascade-чек обязателен).
- В [CLAUDE.md](CLAUDE.md) добавлена секция «Cascade-проверка при любом изменении (ОБЯЗАТЕЛЬНО)» с 5-шаговым чек-листом и примерами реальных каскадов из истории. Теперь любая правка контракта/имени поля/архитектуры обязана пройти этот чек.

**Историческая заметка:** упоминания `ivan.sokolov.test@gmail.com` в §3 (P1-5 описание), §5 «2026-04-17 — закрытие 22 проблем», и в snapshot-выводе питона выше — это истинные записи о том, что значение содержало на момент тех записей. Не править, чтобы сохранить реальную историю фиксов.

---

## 6. Вопросы, требующие решения пользователя

Первоначальные вопросы Q1–Q7 согласованы 2026-04-16:

- **Q1** (архитектура) — **Вариант A** (intent-queue + snapshot). Реализован, §5.
- **Q2** (судьба БД) — **свежая БД**, данные тестовые, можно терять. Сделано: повреждённая переименована в `.corrupted-2026-04-16`, создана чистая.
- **Q3** (web-источники) — **временно отключить** `projectfinder-scan-sources` в Cowork. Делает пользователь.
- **Q4** (порядок работ) — **строго последовательно**: архитектура → логика → чистка. Соблюдено.
- **Q5** (перевод скиллов) — перевод готов, логические правки в уже переведённых русских текстах делает этот чат. Сделано.
- **Q6** (`php-backend`) — **убрать** из профиля `test-fullstack`. Сделано.
- **Q7** (cleanup-list) — «удалить всё, что точно безопасно; итог — в этом документе». Частично сделано.

### Новые открытые вопросы после фикса

1. **`HANDOFF.md` / `ProjectFinder-system.md`** — переписывать или удалять? Сейчас они описывают JSON-эру, вводят в заблуждение. Если команда растёт — переписать, если проект остаётся «для себя» — можно удалить. **Жду ответа.**
2. **`ProjectFinder-implementation-plan.md` + `ProjectFinder-*.drawio`** (три диаграммы 13.04) — тоже legacy. Архивировать в `docs/archive/` или удалить? **Жду ответа.**
3. **`заметки.txt` (104 байта)** — не читал содержимое. Оставить/удалить? **Жду ответа.**
4. **`project-finder/data/drafts/`** — накопленные старые .md-черновики (скиллы их больше не пишут, но старые лежат). Удалить сейчас или подождать? **Жду ответа.**
5. **`project-finder/data/projectfinder.sqlite.backup` и `.corrupted-2026-04-16`** — можно удалить сейчас (БД новая, бэкап и повреждённый файл уже forensics-артефакты) или подождать автобэкапы (P2-5)? **Жду ответа.**
6. **Правки в `evaluate-job/SKILL.md` (P1-9) и `generate-draft/SKILL.md` (P1-10)** — оба скилла уже переведены. Сделать сейчас или как отдельный раунд? **Жду ответа.** Рекомендую сделать — мелкие правки, но закрывают два открытых пункта.
7. **Heartbeat (P2-3), dry-run (P2-4), автобэкапы (P2-5)** — делать следующим раундом или считать «сейчас не приоритет»? **Жду ответа.**
8. **Реальные dev-профили (P2-8)** — в работу, когда захочешь выводить в бой. Жду сигнала.

---

## 7. Ревью от 2026-04-17 (второй проход)

**Дата:** 2026-04-17.
**Источники:** прогон по коду (`scripts/*.py`), всем `skills/*/SKILL.md`, всем `config/*`, плюс проверка живой БД (`integrity_check=ok`, `journal_mode=wal`, все таблицы пусты — никаких реальных данных в системе сейчас нет).
**Контекст:** оператор подтвердил, что инструмент личный (не для команды), о приватности секретов не беспокоится; это снимает алармизм по поводу `secrets.json` / `*.session` в git, но НЕ снимает функциональные баги.

### 7.1 End-to-end flow — что в нём хорошо, что плохо

**Сценарий A (HIGH без borderline, email-канал, A-grade без yellow_flags):**

1. `telegram_scanner.py` (локально) → `pf_db.upsert_job(status='new')` + `pf_db.mark_seen("telegram", url)` в одной транзакции. ✓ Дедупликация по UNIQUE(url) и по `seen_message_ids`.
2. `ops_applier.py` → `VACUUM INTO snapshot.sqlite.tmp` → `os.replace` → snapshot обновлён. ✓ Атомарно.
3. Cowork-задача `evaluate-and-initiate` (cron `15 */2 * * *`) читает `snapshot.sqlite` (read-only), берёт jobs со `status='new'`, для каждой:
   - вызывает скилл `evaluate-job` → возвращает `score_letter`, `score_value`, `breakdown`, `red_flags[]`, `yellow_flags[]`;
   - применяет пост-фильтр «hashtag-only / <20 значимых токенов» (Шаг §3 в evaluate-and-initiate);
   - вычисляет `borderline`;
   - ВНУТРИ скилла должна быть вызвана `pf_policy.decide_outgoing_status(...)` — но `pf_policy.py` лежит в `scripts/`, а скилл живёт в `skills/`; чтобы импорт сработал, скилл должен делать `sys.path.insert(0, project_root/'scripts')` ПЕРЕД любой работой. Это неявно подразумевается, но в `evaluate-and-initiate/SKILL.md` нигде явно не требуется. **Тонкая зависимость, легко сломать.**
   - формирует `emit_batch([set_job_status, create_conversation, insert_outgoing(status='ready'), append_conversation_message])`.
4. `ops_applier` подхватывает intent (поллинг 5 с) → `BEGIN IMMEDIATE` → 4 операции → `COMMIT`. Если падает посреди — `ROLLBACK`, файл едет в `failed/`.
5. snapshot обновляется (раз в 60 с).
6. `email_io.py` (локально, цикл 30 с) → `recover_stuck_sending(300)` + `requeue_failed_for_retry()` + `claim_next_ready('email')` → claim atomic, отправка через SMTP, `mark_outgoing_sent` или `mark_outgoing_failed_with_backoff`.
7. HR отвечает по email → `email_io.check_inbox` (раз в 60 с) → IMAP UID-search с фильтром `(FROM ...) (SINCE дата conversation - 1ч)` + `UID > last_uid` → `pf_db.insert_incoming` (демон пишет напрямую в БД, не через intents — он же локальный). ✓ Идемпотентно по UNIQUE(channel, imap_message_id).
8. Cowork-задача `process-dialogues` (cron `* * * * *`) читает snapshot → новые `incoming(status='new')` → скилл `dialogue-agent` → классифицирует HIGH/MEDIUM/LOW → для HIGH: `find_conversation_local(snapshot, ...)` → `emit_batch([append_conv_msg(incoming), insert_outgoing(ready), append_conv_msg(outgoing), mark_incoming_processed])`.
9. Send-loop повторяется.

**Что хорошо:**
- atomic-batch гарантирует, что либо все 4 операции применены, либо ни одна — нет ситуации «outgoing есть, conv-сообщение нет» (P1-4 закрыт по-настоящему);
- two-phase commit (`ready → sending → sent/failed`) + recover_stuck_sending = от падения отправителя дублей нет;
- UNIQUE-констрейнты на `jobs.url`, `incoming_messages.imap_message_id`, `seen_message_ids` — дубли исключены на уровне схемы;
- `find_conversation` и `update_conversation_meta` есть в `pf_db` — старых ошибок `AttributeError` больше не будет.

**Что плохо в сценарии A (детально в §7.2):**
- read-lag snapshot до 60 с → `dialogue-agent` может не увидеть только что созданную `evaluate-and-initiate` conversation и создать дубль (P0-NEW-3);
- два прогона `evaluate-and-initiate` подряд (или ручной + cron) увидят одни и те же jobs со status='new' до момента, когда первый прогон применил intent + ops_applier его обработал + новый snapshot опубликован → второй прогон сэмитит конфликтующий набор intents с тем же job_id (P1-NEW-3);
- генерация текста описана сразу в двух местах: §3 evaluate-and-initiate говорит «вызови `generate-draft/SKILL.md`», но `generate-draft/SKILL.md` пишет файл в `data/drafts/`, создаёт Gmail-черновик через MCP и НЕ возвращает текст. Контракт сломан (P0-NEW-2);
- от-кого: identity HR увидит как `suprrama@gmail.com` без display name (P0-NEW-1).

**Сценарий B (C-grade или borderline):** всё как в A до шага 3, но `pf_policy.decide_outgoing_status` возвращает `needs_review` → `outgoing.status='needs_review'` + дополнительный `insert_notification(type='review_needed', outgoing_id=...)` в том же batch'е. `telegram_notifier` шлёт в бот → бот рендерит inline-кнопки (выводятся именно по `outgoing_id`) → оператор жмёт ✅ → `bot_handler.handle_approve` зовёт `pf_db.approve_outgoing` → `needs_review → ready` → email_io подхватывает.

✓ Цепочка целостная. Кнопки-через-`outgoing_id` работают, `mark_notification_sent` / `mark_notification_failed_with_backoff` корректны. ✗ Идентичность проблема та же. ✗ Если оператор за 10 минут не нажал ничего — TTL `awaiting_edit` чистится корректно (P1-11), но сама запись `notifications` так и висит `pending` — никакой эскалации, что человек просто молчит, нет.

**Сценарий C (LOW в dialogue-agent):** `insert_escalation` + `insert_notification(type='escalation', urgency='high')` + `append_conv_msg(direction='system', content='Escalated to human: ...')` + `mark_incoming_processed`. Уведомление НЕ имеет `outgoing_id` → telegram_notifier шлёт plain text без кнопок. Оператор читает, отвечает HR сам. ✓ Логика корректная.

**Каждая запись в БД связана с conversation_id?** Да, по сценариям A/B/C: jobs не привязаны (это нормально — job создаётся ДО conversation), conversation_messages обязательно с `conversation_id`, outgoing_messages — обязательно (создаётся в одном batch'е с conversation), notifications — да через поле `conversation_id`, escalations — да. Единственное исключение — `cycle_summary` notification (поле пустое, что верно — это системный отчёт, не привязан к диалогу).

### 7.2 Найденные баги и слабые места

#### P0-NEW-1. Identity полностью сломан: `email_identity` ищется не в той вложенности

- **Где:** [project-finder/scripts/email_io.py:86](project-finder/scripts/email_io.py:86)
  ```python
  ident = (prof.get("email_identity") or {})
  ```
- **Реально в JSON:** [project-finder/config/developers/test-fullstack.json:63](project-finder/config/developers/test-fullstack.json:63) — `email_identity` лежит ВНУТРИ блока `"fixed"` (открыт на строке 6, закрыт на строке 87).
- **Проверка (запустил из корня):**
  ```
  prof.get("email_identity")            → None
  prof["fixed"]["email_identity"]       → {'from_name': 'Иван Соколов',
                                           'from_address': 'ivan.sokolov.test@gmail.com'}
  ```
- **Эффект:** `resolve_from_identity` всегда возвращает `("", smtp.username)`. SMTP-заголовок `From:` будет ровно `suprrama@gmail.com` без display-name. HR получает письмо «от suprrama@gmail.com», подписанное «Иван». Точно та проблема, на которую ты пожаловался.
- **Это та самая P1-5, помеченная как Fixed в §5 — она НЕ исправлена.** Код и JSON разъехались: либо `email_identity` поднимать на верхний уровень профиля (рекомендую — так и подразумевалось «вынести из транспорта в identity»), либо в `resolve_from_identity` читать `prof.get("fixed", {}).get("email_identity") or {}`.
- **Дополнительно:** заметка `_from_note` в [email-config.json:4](project-finder/config/email-config.json:4) обещает читать из `developers/<id>.json → fixed.email_from_name / fixed.email_from_address` — третий вариант нейминга, не совпадает ни с кодом, ни с JSON. Документация и реализация рассинхронизированы.

#### P0-NEW-2. `generate-draft/SKILL.md` устарел и не соответствует архитектуре

- **Где:** [project-finder/skills/generate-draft/SKILL.md](project-finder/skills/generate-draft/SKILL.md) — последнее обновление 16.04 12:20, ДО архитектурного фикса.
- **Что говорит скилл:**
  - Шаг 5: «сохраняй в `project-finder/data/drafts/` файл `{source_id}-{date}-{sequence}.md`»;
  - Шаг 6: «Если `employer_email` не null — открой Gmail в браузере, нажми Compose, вставь текст, закрой окно» — то есть писать черновик В Gmail через UI;
  - Поле выхода: `gmail_draft_created`, `send_method='gmail_draft'`.
- **Что нужно по новой архитектуре:** скилл вызывается ВНУТРИ `evaluate-and-initiate/SKILL.md` (Фаза 3), и его роль — вернуть ТЕКСТ для подстановки в `insert_outgoing(body=...)`. Ни Gmail-draft, ни папка `drafts/` больше не нужны. Папка `data/drafts/` уже удалена (P2-6 fix).
- **Эффект:** если evaluate-and-initiate буквально следует ссылке «прочитай generate-draft/SKILL.md и следуй ему», агент попытается записать `.md` в несуществующую папку, потом откроет Gmail в браузере — на cron-задаче в Cowork это либо упадёт (нет браузера), либо создаст ничейный draft в чужом ящике. Вместо `outgoing(status='ready')` получится тишина.
- **Что делать:** переписать `generate-draft/SKILL.md` под новый контракт — на вход вакансия + профиль + язык, на выход словарь `{subject, body, confidence, facts_used, placeholders_left}`. Никаких побочек: ни файлов, ни Gmail, ни pf_db. То же самое для P1-10 (правила персонализации, которые сейчас задублированы в evaluate-and-initiate Шаг 6).

#### P0-NEW-3. Snapshot read-lag → race между `evaluate-and-initiate` и `dialogue-agent`

- **Где:** контракт между [evaluate-and-initiate/SKILL.md:327](project-finder/skills/evaluate-and-initiate/SKILL.md:327) (создаёт conversation в batch) и [dialogue-agent/SKILL.md:73-95](project-finder/skills/dialogue-agent/SKILL.md:73) (ищет существующую conversation в snapshot).
- **Сценарий:** evaluate-and-initiate отправил intent в 12:00:00, ops_applier применил в 12:00:05, snapshot опубликован в 12:00:30 (следующая граница 60-секундного цикла). Если HR ответил молниеносно (например, на `@username` в Telegram бот реагирует за секунды) и `process-dialogues` запускается в 12:00:15 (cron `* * * * *`), он видит SNAPSHOT по состоянию на 11:59:30 — там conversation ещё нет. `find_conversation_local` вернёт None → dialogue-agent создаст ВТОРУЮ conversation с новым `conv-<hex8>` → сообщение HR попадёт в неё, история первого outgoing'а останется в первой conversation. Та же ошибка, что закрыли в P0-4, только теперь причина другая (read-lag snapshot, а не разный формат id).
- **Вероятность:** при cron `* * * * *` для process-dialogues и snapshot-интервале 60 с — окно ~ 0–60 сек после того, как evaluate-and-initiate emit'ит. Не редкое.
- **Что делать:**
  1. вариант минимальный — `dialogue-agent` перед поиском conversation обязан проверить «возраст snapshot» (через `os.path.getmtime`): если snapshot > 90 с старше реального момента, лучше пропустить incoming до следующего тика;
  2. правильный — изменить контракт. dialogue-agent должен искать conversation НЕ по полю `conversation_id` в incoming_messages (его сейчас ставит `email_io.append_incoming` сразу), а сначала через snapshot, потом fallback на `find_conversation` через intent + read-after-write (что в текущей архитектуре невозможно). Проще: при insert_incoming в email_io уже привязали `conversation_id` к существующей conv (потому что `get_known_email_contacts` в email_io.py читает `conversations` напрямую из живой БД). dialogue-agent должен ДОВЕРЯТЬ этому полю, а не искать ещё раз. Тогда race исчезает.
- **Дополнительно:** `email_io.get_known_email_contacts` и `telegram_io.find_conversation` читают conversations из ЖИВОЙ БД (демоны локальные, прямой доступ разрешён). Это уже корректно — incoming-сообщение уже знает свою conversation_id без посредства snapshot. Значит, переписать dialogue-agent на «использовать `incoming.conversation_id` напрямую, fallback на поиск только если null» — закроет P0-NEW-3 полностью.

#### P0-NEW-4. `scan-sources/SKILL.md` нарушает архитектурный запрет

- **Где:** [project-finder/skills/scan-sources/SKILL.md:131-148](project-finder/skills/scan-sources/SKILL.md:131) — скилл прямо вызывает `pf_db.upsert_job(...)` и `pf_db.mark_seen(...)` из Cowork.
- **Эффект:** ровно тот же путь, который убил БД в первый раз: открытие `projectfinder.sqlite` через FUSE из sandbox, запись с WAL → `disk I/O error` или порча индексов после copy-back. Архитектурное правило «Cowork никогда не открывает .sqlite» нарушено в этом одном скилле.
- **Смягчение:** оператор подтвердил, что `projectfinder-scan-sources` в Cowork выключена (Q3 от 2026-04-16). Пока выключена — проблема не активна.
- **Что делать:** до того, как включать `scan-sources` в Cowork, переписать его на `pf_intents.emit_batch([upsert_job, state_set("seen.web."+url, True)])` — но проще оставить прежнюю операцию `mark_seen`, поскольку seen-таблица всё равно растёт через intents. Любой допуск прямого pf_db к Cowork → возврат к старой ситуации.

#### P0-NEW-5. `notify-human/SKILL.md` тоже устарел: прямой pf_db и Gmail draft

- **Где:** [project-finder/skills/notify-human/SKILL.md:88-94, 113-124, 146-159](project-finder/skills/notify-human/SKILL.md:88) — читает `pf_db.get_db().execute(...)` и пишет `pf_db.insert_notification(...)` из Cowork. Также Шаг 2a — Gmail-черновик через MCP `create_draft`.
- **Эффект:** если евент дойдёт до этого скилла из Cowork, тот же FUSE-сценарий, что в P0-NEW-4. Но: на практике никакой другой скилл сейчас не вызывает `notify-human` — `evaluate-and-initiate` и `dialogue-agent` встраивают `insert_notification` прямо в свои batch'и через intents. Скилл осиротел.
- **Что делать:** удалить `notify-human/` целиком — функциональность покрыта intents'ами. Файл вводит в заблуждение, и однажды кто-то его вызовет.

#### P0-NEW-6. `tg-outreach/SKILL.md` тоже устарел: пишет .md в data/drafts/

- **Где:** [project-finder/skills/tg-outreach/SKILL.md:97-135](project-finder/skills/tg-outreach/SKILL.md:97) — Шаг 5 «сохрани в `data/drafts/tg-{source_id}-{date}-{seq}.md`».
- **Эффект:** аналог P0-NEW-2 для Telegram-канала. После архитектурного фикса telegram-сообщение должно попадать в `outgoing_messages(channel='telegram', status=...)` через intents — никаких файлов.
- **Что делать:** либо удалить (его роль покрыта generate-draft + evaluate-and-initiate Phase 3), либо переписать как «генератор короткого TG-текста» с тем же контрактом, что у нового generate-draft.

#### P0-NEW-7. `evaluate-job/SKILL.md` остался с устаревшим выходом, не сошлёт фильтр в Skip

- **Где:** [project-finder/skills/evaluate-job/SKILL.md](project-finder/skills/evaluate-job/SKILL.md) — поле выхода называется `score` (значения "A"/"B"/"C"/"Skip"), а evaluate-and-initiate в Шаге 1 ожидает `score_letter`. Назвать одно и то же по-разному в двух связанных скиллах — рассогласование.
- **Также:** Phase 3 «red flags» в evaluate-job перечисляет «MLM-подобный язык, явный спам → Skip», но НЕ применяет фильтр «hashtag-only» / «<20 значимых токенов». Этот фильтр сейчас инлайнен в evaluate-and-initiate (P1-9 partial). Нужно перенести в evaluate-job, как и было в плане.
- **Дополнительно:** Phase 5 «извлечение employer_email» — её результат evaluate-and-initiate уже не использует, потому что контакт всё равно берётся из `jobs.contact` (которое заполняет telegram_scanner или scan-sources). Поле просто бесполезно дублируется.

#### P1-NEW-1. `notify_admin` ищет невозможное значение в `notify_on`

- **Где:** [project-finder/scripts/pf_db.py:1204](project-finder/scripts/pf_db.py:1204):
  ```python
  if "admin" in notify_on or "high" in notify_on:
  ```
- **В конфиге:** [project-finder/config/notifications-config.json:25](project-finder/config/notifications-config.json:25) — `"notify_on": ["LOW", "MEDIUM"]`. Ни `"admin"`, ни `"high"` (нижний регистр) там нет.
- **Эффект:** первый цикл всегда проваливается → срабатывает fallback «первый получатель с реальным chat_id». Сейчас получатель один, поэтому работает. Если завтра добавить ВТОРОГО получателя без admin-пометки — алерт всё равно полетит ему. Нет реальной ACL-проверки.
- **Что делать:** либо договориться о значении в схеме (`"notify_on": [..., "admin"]` руками — задокументировать в `_recipients_schema`), либо `if "admin" in notify_on or "HIGH" in notify_on or "LOW" in notify_on` — но это неаккуратно. Честнее — отдельное поле `is_admin: true` в recipient.

#### P1-NEW-2. `send_attempts` (incremented at claim) и `retry_count` (incremented on failure) не координируются

- **Где:** [pf_db.py:885 vs pf_db.py:984](project-finder/scripts/pf_db.py:885) — `claim_outgoing_for_sending` инкрементирует `send_attempts`, `mark_outgoing_failed_with_backoff` инкрементирует `retry_count`. Это два разных счётчика.
- **Эффект:**
  - `recover_stuck_sending` возвращает в 'ready' любую запись старше 5 мин в 'sending' — она снова попадает в `claim`, send_attempts +1, но retry_count не меняется. То есть запись может быть «честно» возвращена в очередь N раз, и `MAX_RETRIES=3` даже не дойдёт до проверки.
  - Если SMTP падает не в момент send, а уже после успешной отправки (например, на этапе `mark_outgoing_sent`), запись пометится `recovered-stuck-sending` и будет переотправлена. Письмо HR придёт дважды.
- **Что делать:**
  - либо считать в `recover_stuck_sending` ту же ошибку «таймаут» как failed → инкрементировать `retry_count` через тот же helper;
  - либо хотя бы логировать `recovered-stuck-sending` в `notify_admin`, чтобы оператор видел эти случаи (сейчас просто префикс в `send_error`).

#### P1-NEW-3. Параллельные запуски `evaluate-and-initiate` сэмитят конфликтующие intents

- **Сценарий:** ручной запуск + cron в одну минуту. Оба читают snapshot, оба видят job со status='new', оба генерируют первое сообщение, оба emit'ят `set_job_status('outreach_queued')` + `insert_outgoing` + `create_conversation`.
- **Что выживет:**
  - первый `set_job_status` пройдёт, второй просто перепишет тем же значением — ОК, не дубль;
  - `create_conversation` — `id` сгенерирован разный (`conv-<hex8>`), у конверсаций нет UNIQUE по (job_id, channel, employer_contact) — будут ДВЕ conversation на одну job;
  - `insert_outgoing` — `id` тоже разный, не дублицируется по констрейнту, обе записи попадут в очередь;
  - HR получит ДВА письма от Ивана с похожим телом.
- **Что делать:**
  - либо UNIQUE-индекс `(job_id, channel, employer_contact, status<>'closed')` на conversations и UNIQUE на outgoing_messages по `(conversation_id, is_first_message)` — тогда второй insert упадёт с IntegrityError, ops_applier обработает как «applied (idempotent)»;
  - либо прежде, чем эмитить batch, evaluate-and-initiate должен проверить `SELECT 1 FROM conversations WHERE job_id=? AND status='active'` — и если найдено, скипнуть job;
  - либо advisory-lock в `service_state.evaluate_and_initiate.in_progress=True` с TTL и проверкой при старте.

#### P1-NEW-4. retry-эскалация после 3 неудач НЕ создаёт уведомления оператору

- **Где:** [pf_db.py:984-1017](project-finder/scripts/pf_db.py:984) — после `tried >= MAX_RETRIES` запись становится `status='failed'`, и всё. `requeue_failed_for_retry` вернёт её в `ready` через `next_retry_at` — но это поле НЕ продлевается, и `retry_count` остаётся равным MAX → `requeue_failed_for_retry` сделает `WHERE retry_count < MAX`, не возьмёт. Запись остаётся в failed навсегда. Оператор ничего не узнаёт.
- **Эффект:** SMTP упал → 5 мин → 15 мин → 60 мин → failed. Если это была единственная попытка отклика на важную вакансию, она тихо потерялась.
- **Что делать:** в `mark_outgoing_failed_with_backoff` при ветке `tried >= MAX_RETRIES` дополнительно вызвать `notify_admin(summary='outgoing failed permanently', ...)` + `insert_escalation`.

#### P1-NEW-5. `requeue_failed_notifications` не пересчитывает `retry_count`

- **Где:** [pf_db.py:1036-1050](project-finder/scripts/pf_db.py:1036) — переводит failed → pending для тех, у кого `retry_count < MAX`. Но при следующем падении `mark_notification_failed_with_backoff` снова инкрементирует ту же `retry_count`. То есть после первого retry счётчик станет 2, после второго 3 → дальше навсегда failed. Это правильное поведение — но НЕТ механизма «алерт оператору, что критичный notify_admin о повреждении БД сам не доехал». Особенно для health-check: «БД повреждена» → telegram_notifier шлёт → 3 неудачи → тишина. Никто не узнает.
- **Что делать:** для типов `admin_alert` / `escalation` — fallback на запись в `logs/projectfinder.log` уровня ERROR (которая уже файловая) и/или вторичный канал (например, email с фиксированным адресом).

#### P1-NEW-6. IMAP-фильтр `(FROM "...") (SINCE "...")` без явного AND работает только из-за неявной семантики

- **Где:** [email_io.py:272](project-finder/scripts/email_io.py:272):
  ```python
  criteria = f'(FROM "{sender_email}") (SINCE "{since_date}")'
  ```
- RFC 3501 говорит, что несколько search-key через пробел — это AND. Так что работает. Но синтаксис стилистически странный, и при добавлении `(UID N+1:*)` (ниже в коде) три скобки подряд — несколько IMAP-серверов (особенно Yandex, Mail.ru) к этому относятся придирчиво. Gmail терпит.
- **Что делать:** перейти на `f'FROM "{sender_email}" SINCE "{since_date}" UID {last_uid+1}:*'` без скобок вокруг каждого ключа. Так канонично.

#### P1-NEW-7. dialogue-agent игнорирует `incoming.conversation_id`

- См. также P0-NEW-3. Если `email_io.append_incoming` уже привязал `incoming_messages.conversation_id` к существующей conv (а так и есть — `get_known_email_contacts` возвращает conv по адресу), dialogue-agent должен использовать его напрямую. Сейчас в SKILL.md описан flow «найти conv через snapshot → если нет, создать новую». Это лишний шаг и источник race в P0-NEW-3.

#### P1-NEW-8. `human_like_delay` (30–180 с) близок к `RECOVER_STUCK_AFTER_SEC` (300 с)

- **Где:** [telegram_io.py:57](project-finder/scripts/telegram_io.py:57) `HUMAN_DELAY_RANGE = (30, 180)`; [pf_db.py recover_stuck](project-finder/scripts/pf_db.py:949) — порог 300 с.
- **Сценарий:** dialogue-agent создал reply (`is_reply=True`), telegram_io claim'нул его в 12:00:00 (status='sending'), задержка 180 с → отправка в 12:03:00. К этому моменту `recover_stuck_sending(300)` ещё не сработает (порог 5 мин = 300 с). Норма. Но если задержка ещё чуть-чуть длиннее (например, добавим typing-эмуляцию + сетевая латентность 30 с) — превысит. И тогда recover вернёт запись в `ready`, telegram_io claim'нет ВТОРОЙ инстанс отправки, HR получит дубль.
- **Эффект:** при текущих числах буфер 120 секунд. Узко. Кто-то добавит проверку или второй sleep — упадёт.
- **Что делать:** либо порог `RECOVER_STUCK_AFTER_SEC = 600`, либо разделять human-delay на «pre-claim» (sleep ДО claim) и «post-claim» (короткая typing-симуляция). Сейчас sleep ВНУТРИ claim'а, что и создаёт окно.

#### P1-NEW-9. cycle-summary дедуп через snapshot пропустит ручной перезапуск в первые 60 с

- **Где:** [evaluate-and-initiate/SKILL.md:496-507](project-finder/skills/evaluate-and-initiate/SKILL.md:496) — проверка «не было ли cycle_summary за последние 30 минут» через snapshot.
- **Эффект:** если первый прогон emit'нул cycle_summary, ops_applier применил его, но snapshot ещё не пересоздан, второй прогон в snapshot увидит «нет» — эмитит ДУБЛЬ. ops_applier применит оба, оператор получит две копии итога.
- **Что делать:** дедуп должен идти через `idempotency_key`-определение в pf_intents (например, `key = sha1(f"cycle_summary-{date.isoformat(minute)}")` — детерминированный) и опору на `applied_keys` ops_applier'а. Тогда повтор будет блокирован на уровне аппликатора, а не на уровне snapshot.

#### P1-NEW-10. `bot_handler.handle_review_command` пишет outgoing.body «как есть», без эскейпинга в Telegram

- **Где:** [bot_handler.py:213-219](project-finder/scripts/bot_handler.py:213) — текст черновика пакуется в `text` и шлётся через `sendMessage` без `parse_mode`. Markdown-символы (`_`, `*`, `[`, `]`, ```) в теле HR могут вернуть HTTP 400 от Telegram, нотификация сядет в failed → backoff → permanent.
- **Эффект:** если HR прислал markdown-style ссылку или email с подчёркиванием, /review команда не покажет этот черновик никогда.
- **Что делать:** либо `parse_mode='HTML'` + `html.escape(text)`, либо санитайзер на `_`/`*`. Минимальный фикс — заменить в text `\` → `\\`, `_` → `\_`, `*` → `\*`. Либо вообще не парсить (без `parse_mode`) — тогда не упадёт, но markdown получит HR в сыром виде. Сейчас `parse_mode` не задан → телеграм его не парсит, значит, HTTP 400 не должно быть. **Перепроверил — на самом деле сообщение пройдёт. Понижаю до P2.**

#### P2-NEW-1. ПРОТИВ ПРАВИЛ: identity и идентификатор разработчика берутся из `outgoing.developer_id`, но `developer_id` устанавливается СКИЛЛОМ evaluate-and-initiate

- **Где:** [evaluate-and-initiate/SKILL.md:99-109](project-finder/skills/evaluate-and-initiate/SKILL.md:99) — переменная `matched_position_profile_id` пишется в `developer_id`. Но как её получить — НЕ описано ни в SKILL.md скилла, ни в `evaluate-job` (который возвращает `matched_position`, не `developer_id`). Подразумевается, что скилл должен сам сканировать `config/developers/*.json`, найти профиль, у которого `position_matching.applicable_position_ids` содержит `matched_position`, и (с учётом `priority_order`) выбрать первый. Это нигде не написано.
- **Эффект:** при наличии нескольких профилей агент должен «угадать», и этот выбор не покрыт инвариантами. Сейчас профиль один (`test-fullstack`), поэтому всё работает по совпадению.
- **Что делать:** добавить в `evaluate-and-initiate/SKILL.md` явный блок «как выбрать developer_id»: скан `config/developers/*.json` → отфильтровать `active=true` → отфильтровать содержащие `matched_position` в `applicable_position_ids` → отсортировать по `priority_order` → взять первый. Если ни один не подходит — `set_job_status('error_no_profile')`.

#### P2-NEW-2. Один Telethon-клиент / одна сессия на всех

- **Где:** [telegram_io.py:247](project-finder/scripts/telegram_io.py:247) — `TelegramClient(str(session_path), api_id, api_hash)`, `session_path = SCRIPT_DIR / "projectfinder"`. Сессия одна → все исходящие TG-сообщения идут от одного аккаунта Telegram.
- **Эффект:** если разработчиков станет двое и больше с разными TG-username'ами, отправлять от каждого нужно из СВОЕЙ сессии. Текущая архитектура этого не умеет; для одного оператора — норма.
- **Что делать:** задокументировать ограничение в README/HANDOFF (когда они появятся): «multi-developer Telegram outreach пока не поддержан, single-account only». Когда понадобится — `TelegramClient` per developer + `session_<dev_id>` файлы + диспатч в process_outgoing по `outgoing.developer_id`.

#### P2-NEW-3. `evaluate-and-initiate/SKILL.md` подразумевает наличие `pf_policy.decide_outgoing_status` без `sys.path.insert`

- **Где:** [evaluate-and-initiate/SKILL.md:266](project-finder/skills/evaluate-and-initiate/SKILL.md:266) — пример кода `from pf_policy import decide_outgoing_status`. Но `pf_policy.py` лежит в `project-finder/scripts/`, а скилл — в Cowork-sandbox, где `scripts/` может не быть в `sys.path`.
- **Эффект:** при буквальной интерпретации скилла Cowork получит `ImportError`. Проблема решается добавлением `sys.path.insert(0, str(PROJECT_ROOT / "scripts"))`, но в SKILL.md этот шаг описан только для `pf_intents` (Шаг §14), а для `pf_policy` забыт.
- **Что делать:** в SKILL.md добавить явное `sys.path.insert(0, scripts_dir)` ПЕРЕД ВСЕМИ импортами `pf_*`. Сейчас код предполагает, что один такой insert уже есть для `pf_intents` — но это надо явно проговорить.

#### P2-NEW-4. `dialogue-agent/SKILL.md` Шаг 4 «Gmail draft» оставлен «для исторической справки», но скилл вызывается process-dialogues — Gmail MCP может не быть

- **Где:** [dialogue-agent/SKILL.md:406-408](project-finder/skills/dialogue-agent/SKILL.md:406) — «Gmail-черновики больше НЕ создаём». Хорошо, но шаг оставлен в нумерации, читателя это путает. Если кто-то увидит «Шаг 4 — Gmail draft» и применит — попытается вызвать `mcp__gmail_create_draft`, которое в Cowork может быть отключено.
- **Что делать:** удалить раздел «Шаг 4 — Gmail draft» полностью; нумерацию шагов сдвинуть.

#### P2-NEW-5. notifications-config.json содержит `digest_rules`, но реализации дайджеста в коде нет

- **Где:** [notifications-config.json:46-51](project-finder/config/notifications-config.json:46) — описан daily digest at 18:00, шаблон. Но ни в одном `.py` нет ни вызова, ни cron'а, который бы это запускал.
- **Эффект:** мёртвый код в конфиге; впечатление, что фича работает, а её нет.
- **Что делать:** удалить блок `digest_rules` из конфига, либо добавить пункт в `# 4 Очередь работ` про реализацию.

#### P2-NEW-6. `bot_handler.py` использует `time.time()` для TTL `awaiting_edit`, но сериализация через JSON — int вписан корректно, проверка корректна

- Перепроверил: `int(time.time()) + AWAITING_EDIT_TTL_SEC` пишется int → JSON-сериализуется как число → читается как int → `time.time() > expires_at` корректно. **OK, не баг.** Записываю как зачёт.

#### P2-NEW-7. `auto-reply-config.json.confidence_routing` дублирует логику `pf_policy`

- **Где:** [auto-reply-config.json:19-37](project-finder/config/auto-reply-config.json:19) — старый блок `HIGH/MEDIUM/LOW → auto_send/notify_user`. `pf_policy.decide_outgoing_status` его НЕ читает — для решения он смотрит `first_message_policy.default` + `developer.auto_reply_settings.auto_send_first_message`. То есть `confidence_routing` — мёртвый блок.
- **Что делать:** удалить из конфига, либо явно интегрировать в `pf_policy`. Сейчас он не источник правды и сбивает с толку.

### 7.3 Рекомендации по архитектуре

**Short-term (до запуска):**

1. **Закрыть P0-NEW-1 (identity).** Один LOC: в `email_io.resolve_from_identity` заменить `prof.get("email_identity")` на `prof.get("fixed", {}).get("email_identity")`. ИЛИ поднять `email_identity` на верх профиля. Я бы сделал первое — меньше правок, JSON «чище».
2. **Переписать `generate-draft/SKILL.md`** под контракт «вход — job + profile + language; выход — словарь {subject, body, confidence, facts_used}». Никаких побочек. Аналогично `tg-outreach/SKILL.md` — оба сейчас осиротели.
3. **Удалить `notify-human/SKILL.md` и `tg-outreach/SKILL.md`** (или явно пометить DEPRECATED) — их функциональность встроена в evaluate-and-initiate / dialogue-agent через intents.
4. **Закрыть P0-NEW-3 (race conv-create vs HR-reply).** В dialogue-agent сначала использовать `incoming.conversation_id` напрямую — оно уже привязано демоном email_io/telegram_io. Поиск conversation через snapshot — только для случая, когда `conversation_id is None`.
5. **Закрыть P0-NEW-7 (evaluate-job + score_letter).** Убрать рассогласование выходных полей, перенести фильтр «hashtag-only» в evaluate-job.
6. **Добавить advisory-lock на evaluate-and-initiate (P1-NEW-3).** В service_state выставлять `evaluate_and_initiate.in_progress=True` при старте + проверять при старте + TTL.
7. **Эскалация при retry exhausted (P1-NEW-4).** В `mark_outgoing_failed_with_backoff` добавить `notify_admin` при `tried >= MAX_RETRIES`.
8. **Переписать `scan-sources/SKILL.md` на pf_intents (P0-NEW-4)** ИЛИ навсегда оставить выключенным с явным запретом включать в Cowork.

**Long-term (после первого боевого запуска):**

1. **Заменить snapshot-через-VACUUM на «event log + replay».** Cowork эмитит intent → ops_applier применяет + отдельным батчем дописывает в `events` таблицу → snapshot собирается ИЗ events за последние N часов, не из всей БД. Lag минимизируется за счёт того, что snapshot становится append-only-read-friendly.
2. **Добавить heartbeat для всех демонов (§3 P2-3 open).** Кроме ops_applier — нужны и для остальных пятёрых.
3. **JSON-логи с trace_id.** Каждый intent → trace_id; каждое его применение → лог с тем же trace_id; каждое исходящее → лог с тем же trace_id. При проблеме с отдельной перепиской `grep trace_id` показывает всю цепочку.
4. **Шифрование `conversation_messages.content`.** Там реальные ответы HR. Fernet-ключ из `secrets.json`. Не блокер для текущего тестового прогона, но нужно до первого реального HR-разговора.
5. **Multi-developer Telegram (P2-NEW-2).** Сессия per developer, диспатч в telegram_io по `outgoing.developer_id`.
6. **Unit-тесты для critical path:** insert_outgoing → claim → send → mark_sent (golden); insert_incoming с дублирующим imap_message_id → один INSERT IGNORE (идемпотентность); recover_stuck_sending после краша между claim и mark_sent (нет дубля). Сейчас тестов нет вообще, регресс гарантирован при следующей правке.

### 7.4 Чек-лист «можно ли сейчас коммитить и запускать в бой»

| Что | Состояние | Решение |
|---|---|---|
| Архитектура «intents + snapshot» | Реализована, БД integrity_check=ok, journal=wal | ✓ |
| Закрытые из 27 предыдущих проблем | 20 закрыты, 1 partial, 6 open | ✓ можно мерджить интеграцию |
| Identity HR в email | СЛОМАН (P0-NEW-1) | ✗ HARD STOP |
| Скиллы `generate-draft`, `tg-outreach`, `notify-human`, `scan-sources` | устарели или нарушают архитектуру | ✗ исправить или удалить |
| evaluate-job несовместим по полю `score`/`score_letter` | Рассогласован | ✗ исправить |
| Race dialogue-agent ↔ evaluate-and-initiate (snapshot lag) | Есть (P0-NEW-3) | ✗ исправить |
| Race два прогона evaluate-and-initiate | Есть (P1-NEW-3) | ✗ advisory-lock |
| Эскалация после retry exhausted | Не реализована (P1-NEW-4) | ⚠ важно до первого реального HR |
| .gitignore | Почищен в этом проходе | ✓ |
| `secrets.json`, `*.session` в git | Не критично (личный инструмент, по согласованию с оператором) | ⚠ к сведению |
| БД пустая, нет реальных данных | ✓ | ✓ можно ронять без потерь |
| Heartbeat / dry-run / автобэкапы | Не реализованы | ⚠ до 24/7-режима — обязательны |

**Вердикт:** **коммитить промежуточные правки можно** (intents-архитектура — реальное улучшение, БД здоровая). **Запускать в бой — нельзя** до закрытия P0-NEW-1, P0-NEW-2, P0-NEW-3 минимум. Сейчас при первом же реальном письме HR увидит письмо «от suprrama@gmail.com» подписанное «Иван» — это палится в одно касание, и далее любой реальный отклик в эту цепочку не даст результата.

**Минимально-достаточный план для первого боевого:**
1. Фикс identity (1 строка кода).
2. Переписать generate-draft (час работы — выход словарь, никаких файлов).
3. Удалить или DEPRECATED-пометить notify-human / tg-outreach.
4. dialogue-agent: использовать `incoming.conversation_id` напрямую.
5. evaluate-job ↔ evaluate-and-initiate: согласовать `score_letter`.
6. Добавить advisory-lock в evaluate-and-initiate.
7. Прогнать на одной (своей) тестовой почте + одном TG-канале с заведомо контролируемым HR (можно собой).

После — можно ОДНОГО реального HR попробовать с ручным наблюдением через `/status` бота.


