#!/usr/bin/env python3
"""
ProjectFinder — intent-queue writer (Cowork-side helper).

Cowork-скиллы НЕ открывают `projectfinder.sqlite` напрямую: SQLite+WAL на FUSE
даёт `disk I/O error`, а copy-back портит индексы и затирает данные локальных
демонов. Вместо этого скилл вызывает `pf_intents.emit(operation, params)`,
который атомарно кладёт JSON-файл в `data/intents/pending/`. Локальный демон
`ops_applier.py` подхватывает, применяет через `pf_db.<operation>(**params)`
и переносит файл в `applied/` или `failed/`.

Запись JSON — через tmp + `os.rename`: атомарная операция, работает на FUSE,
гонок нет.

Использование:

    import pf_intents
    key = pf_intents.emit("insert_outgoing", {
        "conversation_id": conv_id,
        "job_id": job_id,
        "channel": "email",
        "recipient": "hr@acme.com",
        "subject": "Fullstack role",
        "body": draft_text,
        "status": "ready",
        "is_first_message": True,
        "confidence": "HIGH",
    })
    # key — idempotency_key. Если Cowork упал до rename, повторный запуск
    # сгенерит новый key для той же операции — это ок; применение в
    # ops_applier всё равно идемпотентно на уровне UNIQUE-констрейнтов БД
    # (url, imap_message_id и т.д.).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
INTENTS_DIR = DATA_DIR / "intents"
INTENTS_PENDING = INTENTS_DIR / "pending"
INTENTS_TMP = INTENTS_DIR / ".tmp"
# applied/ и failed/ создаёт сам ops_applier при первом перемещении — здесь
# не трогаем, чтобы Cowork-скилл не делал ничего в этих папках.


# ---------------------------------------------------------------------------
# Whitelist допустимых операций
# ---------------------------------------------------------------------------
#
# Оба конца (эмиттер и аппликатор) должны знать один и тот же список. Здесь
# мы проверяем, что скилл не пытается протолкнуть произвольный op, который
# ops_applier всё равно отбросит. Раннее падение на стороне Cowork — дешевле
# и виднее, чем файл в failed/, который надо разбирать вручную.

ALLOWED_OPERATIONS = frozenset({
    # jobs
    "upsert_job",
    "set_job_status",
    # conversations
    "create_conversation",
    "set_conversation_status",
    "touch_conversation",
    "update_conversation_meta",
    "append_conversation_message",
    # incoming (крайне редко из Cowork; обычно пишут демоны)
    "insert_incoming",
    "mark_incoming_processed",
    # outgoing
    "insert_outgoing",
    "approve_outgoing",
    "reject_outgoing",
    "update_outgoing_body",
    # notifications
    "insert_notification",
    "ack_notification",
    "notify_admin",
    # escalations
    "insert_escalation",
    "resolve_escalation",
    # service_state
    "state_set",
    # batched composite (ops_applier выполнит список операций в одной
    # транзакции — нужно для «создать conversation и outgoing атомарно»)
    "batch",
})


class IntentError(RuntimeError):
    pass


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dirs() -> None:
    INTENTS_PENDING.mkdir(parents=True, exist_ok=True)
    INTENTS_TMP.mkdir(parents=True, exist_ok=True)


def _validate(operation: str, params: dict, batch_ops: Optional[list]) -> None:
    if operation not in ALLOWED_OPERATIONS:
        raise IntentError(
            f"unknown operation {operation!r}; allowed: {sorted(ALLOWED_OPERATIONS)}"
        )
    if not isinstance(params, dict):
        raise IntentError("params must be a dict")
    if operation == "batch":
        if not isinstance(batch_ops, list) or not batch_ops:
            raise IntentError("batch intent requires non-empty 'ops' list")
        for sub in batch_ops:
            if not isinstance(sub, dict):
                raise IntentError("each batch op must be a dict")
            sub_op = sub.get("operation")
            if sub_op not in ALLOWED_OPERATIONS or sub_op == "batch":
                raise IntentError(f"batch contains invalid sub-op: {sub_op!r}")
            if not isinstance(sub.get("params", {}), dict):
                raise IntentError("batch op.params must be a dict")


def emit(operation: str, params: Optional[dict] = None, *,
         source: str = "skill",
         ops: Optional[list[dict]] = None,
         idempotency_key: Optional[str] = None) -> str:
    """
    Write one intent to `data/intents/pending/<uuid>.json`.
    Returns the idempotency_key (uuid hex). Raises IntentError on invalid input.

    Для `operation='batch'` передай список суб-операций в `ops`:
        pf_intents.emit("batch", {}, ops=[
            {"operation": "create_conversation", "params": {...}},
            {"operation": "insert_outgoing", "params": {...}},
        ])
    Они применяются в одной транзакции на стороне ops_applier — либо все
    проходят, либо все откатываются.
    """
    params = params or {}
    _validate(operation, params, ops)

    _ensure_dirs()

    key = idempotency_key or uuid.uuid4().hex
    intent = {
        "idempotency_key": key,
        "created_at": _utcnow_iso(),
        "source": source,
        "operation": operation,
        "params": params,
    }
    if operation == "batch":
        intent["ops"] = ops

    # tmp-файл на той же FS, что и pending/, чтобы `os.rename` был атомарным.
    tmp_name = f"{int(time.time() * 1000)}-{key}.json.tmp"
    tmp_path = INTENTS_TMP / tmp_name
    final_name = f"{int(time.time() * 1000)}-{key}.json"
    final_path = INTENTS_PENDING / final_name

    # ensure_ascii=False — кириллица в сообщениях читается, отладка дешевле.
    data = json.dumps(intent, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, final_path)  # atomic within same filesystem
    return key


def emit_batch(ops: list[dict], *, source: str = "skill",
               idempotency_key: Optional[str] = None) -> str:
    """Shortcut: emit a batch of ops in one transaction."""
    return emit("batch", {}, ops=ops, source=source,
                idempotency_key=idempotency_key)


# ---------------------------------------------------------------------------
# Диагностика — сколько сейчас в очереди, чтобы скилл мог отчитаться.
# ---------------------------------------------------------------------------

def pending_count() -> int:
    if not INTENTS_PENDING.exists():
        return 0
    return sum(1 for p in INTENTS_PENDING.iterdir() if p.suffix == ".json")


if __name__ == "__main__":
    # Smoke test — эмитим безвредный state_set и проверяем, что файл лёг.
    key = emit("state_set", {
        "key": "pf_intents_smoke",
        "value": {"at": _utcnow_iso()},
    }, source="pf_intents.smoke")
    print(f"emitted key={key}; pending={pending_count()}")
    print(f"pending dir: {INTENTS_PENDING}")
