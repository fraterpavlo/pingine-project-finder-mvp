#!/usr/bin/env python3
"""
ProjectFinder — ops_applier daemon.

Единственный писатель в `project-finder/data/projectfinder.sqlite` со стороны
Cowork-скиллов. Читает intent-файлы из `data/intents/pending/`, диспатчит
каждый в соответствующую функцию `pf_db.<operation>(**params)`, переносит
применённый intent в `applied/` (или `failed/`, если операция бросила
исключение).

Побочная обязанность — раз в `SNAPSHOT_INTERVAL_SEC` публиковать
`data/snapshot.sqlite` как read-only копию БД в DELETE-mode, которую
Cowork-скиллы читают без FUSE-проблем. Публикация атомарна:
`VACUUM INTO snapshot.tmp` → `os.replace` → `snapshot.sqlite`.

Почему это решение (а не journal_mode=DELETE + прямой доступ):
- один писатель = нет гонок по WAL и fcntl-локам через FUSE;
- intent-файлы видны глазами — дебаг элементарный;
- идемпотентность через idempotency_key в JSON → можно безопасно пересылать
  один и тот же intent несколько раз, аппликатор применит ровно один раз.

Контракт формата intent-файла см. `pf_intents.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, Exception):
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import pf_db          # noqa: E402
import pf_intents     # noqa: E402


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

POLL_INTERVAL_SEC = 5           # с какой частотой ищем новые intents
SNAPSHOT_INTERVAL_SEC = 60      # с какой частотой публикуем snapshot.sqlite
APPLIED_KEYS_LIMIT = 2000       # сколько последних idempotency_key помним

DATA_DIR = pf_intents.DATA_DIR
INTENTS_DIR = pf_intents.INTENTS_DIR
PENDING_DIR = pf_intents.INTENTS_PENDING
APPLIED_DIR = INTENTS_DIR / "applied"
FAILED_DIR = INTENTS_DIR / "failed"

SNAPSHOT_PATH = DATA_DIR / "snapshot.sqlite"
SNAPSHOT_TMP = DATA_DIR / "snapshot.sqlite.tmp"

STATE_KEY_APPLIED = "ops_applier.applied_keys"   # rolling set в service_state
STATE_KEY_HEARTBEAT = "heartbeat.ops_applier"    # для будущего health-check

_shutdown = False


# ---------------------------------------------------------------------------
# Логгер (тот же стиль, что у остальных демонов)
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [ops_app ] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Маппинг operation → pf_db.<func>
# ---------------------------------------------------------------------------
#
# Для каждой операции задаём, как распаковать `params`. Для большинства функций
# pf_db это просто `func(params_dict)` или `func(**params)`. Разные сигнатуры
# учтены индивидуально, без reflection'а — надёжнее читать код.

def _op_upsert_job(p):          return pf_db.upsert_job(p)
def _op_set_job_status(p):      return pf_db.set_job_status(p["job_id"], p["status"], p.get("match"))
def _op_create_conversation(p): return pf_db.create_conversation(p)
def _op_set_conv_status(p):     return pf_db.set_conversation_status(p["conversation_id"], p["status"])
def _op_touch_conversation(p):  return pf_db.touch_conversation(p["conversation_id"])
def _op_update_conv_meta(p):    return pf_db.update_conversation_meta(p["conversation_id"], p["meta"])
def _op_append_conv_msg(p):     return pf_db.append_conversation_message(p["conversation_id"], p["msg"])
def _op_insert_incoming(p):     return pf_db.insert_incoming(p)
def _op_mark_incoming_proc(p):  return pf_db.mark_incoming_processed(p["incoming_id"])
def _op_insert_outgoing(p):     return pf_db.insert_outgoing(p)
def _op_approve_outgoing(p):    return pf_db.approve_outgoing(p["outgoing_id"], p.get("edited_body"), p.get("edited_by"))
def _op_reject_outgoing(p):     return pf_db.reject_outgoing(p["outgoing_id"], p.get("notes"))
def _op_update_out_body(p):     return pf_db.update_outgoing_body(p["outgoing_id"], p["body"], p.get("edited_by", "human"))
def _op_insert_notification(p): return pf_db.insert_notification(p)
def _op_ack_notification(p):    return pf_db.ack_notification(p["notif_id"])
def _op_notify_admin(p):        return pf_db.notify_admin(
                                    p["summary"], p["message"],
                                    urgency=p.get("urgency", "normal"),
                                    type_=p.get("type", "admin_alert"),
                                    job_id=p.get("job_id"),
                                    conversation_id=p.get("conversation_id"),
                                )
def _op_insert_escalation(p):   return pf_db.insert_escalation(p)
def _op_resolve_escalation(p):  return pf_db.resolve_escalation(p["escalation_id"], p.get("status", "resolved"))
def _op_state_set(p):           return pf_db.state_set(p["key"], p["value"])


DISPATCH: dict[str, Callable[[dict], Any]] = {
    "upsert_job":                 _op_upsert_job,
    "set_job_status":             _op_set_job_status,
    "create_conversation":        _op_create_conversation,
    "set_conversation_status":    _op_set_conv_status,
    "touch_conversation":         _op_touch_conversation,
    "update_conversation_meta":   _op_update_conv_meta,
    "append_conversation_message": _op_append_conv_msg,
    "insert_incoming":            _op_insert_incoming,
    "mark_incoming_processed":    _op_mark_incoming_proc,
    "insert_outgoing":            _op_insert_outgoing,
    "approve_outgoing":           _op_approve_outgoing,
    "reject_outgoing":            _op_reject_outgoing,
    "update_outgoing_body":       _op_update_out_body,
    "insert_notification":        _op_insert_notification,
    "ack_notification":           _op_ack_notification,
    "notify_admin":               _op_notify_admin,
    "insert_escalation":          _op_insert_escalation,
    "resolve_escalation":         _op_resolve_escalation,
    "state_set":                  _op_state_set,
}


def _apply_single(op: str, params: dict) -> Any:
    fn = DISPATCH.get(op)
    if fn is None:
        raise KeyError(f"unknown operation {op!r}")
    return fn(params)


# ---------------------------------------------------------------------------
# Rolling set применённых idempotency_key (чтобы дубли не применялись)
# ---------------------------------------------------------------------------

def _load_applied_keys() -> list[str]:
    val = pf_db.state_get(STATE_KEY_APPLIED, default=[])
    if not isinstance(val, list):
        return []
    return val


def _save_applied_keys(keys: list[str]) -> None:
    # Ограничиваем длину, чтобы service_state не разрастался бесконечно.
    if len(keys) > APPLIED_KEYS_LIMIT:
        keys = keys[-APPLIED_KEYS_LIMIT:]
    pf_db.state_set(STATE_KEY_APPLIED, keys)


# ---------------------------------------------------------------------------
# Работа с файлами intent'ов
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    APPLIED_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)


def _list_pending() -> list[Path]:
    if not PENDING_DIR.exists():
        return []
    # Имена начинаются с timestamp-ms, значит сортировка по имени = по времени.
    return sorted(p for p in PENDING_DIR.iterdir() if p.suffix == ".json")


def _read_intent(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"cannot parse {path.name}: {e}")
        return None
    if not isinstance(data, dict):
        log(f"{path.name}: top-level not a dict")
        return None
    return data


def _move(path: Path, target_dir: Path, suffix: str = "") -> None:
    """Переместить файл с уникальным именем в target_dir.
    suffix — опциональная метка (например, '.error'), добавляется перед .json
    """
    name = path.stem + suffix + path.suffix
    target = target_dir / name
    # Если файл уже существует (редкий случай при двух параллельных
    # аппликаторах) — добавляем ещё timestamp.
    if target.exists():
        target = target_dir / f"{path.stem}{suffix}-{int(time.time()*1000)}{path.suffix}"
    os.replace(str(path), str(target))


# ---------------------------------------------------------------------------
# Обработка одного intent'а
# ---------------------------------------------------------------------------

def _apply_intent(intent: dict, applied_keys: set[str]) -> tuple[str, str]:
    """
    Применить один intent. Возвращает (outcome, note):
      outcome ∈ {"applied", "duplicate", "failed"}
      note    — произвольный человеко-читаемый комментарий.
    """
    key = intent.get("idempotency_key")
    op = intent.get("operation")
    params = intent.get("params") or {}

    if not key or not isinstance(key, str):
        return "failed", "no idempotency_key"
    if key in applied_keys:
        return "duplicate", f"key {key[:8]} already applied"

    if op == "batch":
        sub_ops = intent.get("ops") or []
        if not isinstance(sub_ops, list) or not sub_ops:
            return "failed", "batch with empty ops"
        try:
            with pf_db.transaction():
                for sub in sub_ops:
                    _apply_single(sub["operation"], sub.get("params", {}))
            applied_keys.add(key)
            return "applied", f"batch x{len(sub_ops)}"
        except Exception as e:
            tb = traceback.format_exc().splitlines()[-1]
            return "failed", f"batch error: {e} ({tb})"

    if op not in DISPATCH:
        return "failed", f"unknown operation {op!r}"

    try:
        _apply_single(op, params)
        applied_keys.add(key)
        return "applied", op
    except sqlite3.IntegrityError as e:
        # IntegrityError у SQLite покрывает РАЗНЫЕ нарушения. UNIQUE — это
        # «такая запись уже есть» (идемпотентно, можно проглотить).
        # NOT NULL / FOREIGN KEY / CHECK — это РЕАЛЬНЫЕ ошибки данных,
        # operation не выполнен, проглатывать НЕЛЬЗЯ — иначе оператор
        # никогда не узнает (был случай со state_set(value=None) → NOT NULL,
        # ops_applier пометил applied, advisory-lock остался висеть навсегда).
        msg = str(e).lower()
        is_unique_dup = "unique" in msg
        if is_unique_dup:
            applied_keys.add(key)
            return "applied", f"{op} (UNIQUE — idempotent: {e})"
        # Не UNIQUE — реальная ошибка. НЕ помечаем applied, файл уйдёт в failed/.
        return "failed", f"{op} IntegrityError: {e}"
    except Exception as e:
        tb = traceback.format_exc().splitlines()[-1]
        return "failed", f"{op} error: {e} ({tb})"


# ---------------------------------------------------------------------------
# Главный цикл аппликатора
# ---------------------------------------------------------------------------

def process_pending(applied_keys: set[str]) -> dict:
    """Обработать все текущие pending intents. Возвращает счётчики."""
    stats = {"applied": 0, "duplicate": 0, "failed": 0}
    for path in _list_pending():
        if _shutdown:
            break
        intent = _read_intent(path)
        if intent is None:
            _move(path, FAILED_DIR, suffix=".parse-error")
            stats["failed"] += 1
            continue

        outcome, note = _apply_intent(intent, applied_keys)
        stats[outcome] += 1

        if outcome == "applied":
            log(f"applied {path.name} — {note}")
            _move(path, APPLIED_DIR)
        elif outcome == "duplicate":
            log(f"duplicate {path.name} — {note}")
            _move(path, APPLIED_DIR, suffix=".duplicate")
        else:
            log(f"FAILED  {path.name} — {note}")
            _move(path, FAILED_DIR, suffix=".error")

    return stats


def publish_snapshot() -> None:
    """VACUUM INTO → os.replace. Атомарно, независимо от того, идёт ли сейчас
    запись в оригинал. Snapshot — в DELETE-mode, без WAL/SHM-спутников, поэтому
    Cowork-скилл может его спокойно `sqlite3.connect(..., uri='mode=ro')`.
    """
    if SNAPSHOT_TMP.exists():
        try:
            SNAPSHOT_TMP.unlink()
        except Exception:
            pass
    # VACUUM INTO выполняется от того же соединения, что и остальной pf_db,
    # поэтому получает консистентный снимок на момент запуска.
    conn = pf_db.get_db()
    try:
        conn.execute(f"VACUUM INTO '{SNAPSHOT_TMP.as_posix()}'")
    except sqlite3.OperationalError as e:
        log(f"VACUUM INTO failed: {e}")
        return
    try:
        os.replace(str(SNAPSHOT_TMP), str(SNAPSHOT_PATH))
    except Exception as e:
        log(f"replace snapshot failed: {e}")


def watch_loop(interval: int = POLL_INTERVAL_SEC) -> None:
    pf_db.init_db()
    _ensure_dirs()
    applied_keys: set[str] = set(_load_applied_keys())
    log(f"started; pending={pf_intents.pending_count()}, "
        f"remembered_keys={len(applied_keys)}, "
        f"journal_mode={pf_db.CURRENT_JOURNAL_MODE}")

    last_snapshot = 0.0
    last_keys_flush = 0.0
    while not _shutdown:
        stats = process_pending(applied_keys)
        if stats["applied"] or stats["failed"]:
            log(f"cycle: applied={stats['applied']} dup={stats['duplicate']} "
                f"failed={stats['failed']}")

        now = time.time()
        # Периодически сбрасываем applied-keys в БД — чтобы перезапуск
        # аппликатора не пытался заново применить уже применённое.
        if now - last_keys_flush >= 30 and applied_keys:
            _save_applied_keys(sorted(applied_keys)[-APPLIED_KEYS_LIMIT:])
            last_keys_flush = now

        # Публикуем snapshot раз в SNAPSHOT_INTERVAL_SEC.
        if now - last_snapshot >= SNAPSHOT_INTERVAL_SEC:
            publish_snapshot()
            last_snapshot = now

        # Heartbeat в service_state — пригодится для /status и
        # будущего health-check.
        try:
            pf_db.state_set(STATE_KEY_HEARTBEAT, pf_db.utcnow_iso())
        except Exception:
            pass

        time.sleep(interval)

    # Финальный flush перед выходом.
    _save_applied_keys(sorted(applied_keys)[-APPLIED_KEYS_LIMIT:])
    log("stopping")


def run_once() -> dict:
    """Разовый прогон — полезен для CLI `--once` и тестов."""
    pf_db.init_db()
    _ensure_dirs()
    applied_keys: set[str] = set(_load_applied_keys())
    stats = process_pending(applied_keys)
    _save_applied_keys(sorted(applied_keys)[-APPLIED_KEYS_LIMIT:])
    publish_snapshot()
    return stats


def _handle_signal(signum, frame) -> None:
    global _shutdown
    _shutdown = True


def main() -> None:
    parser = argparse.ArgumentParser(description="ProjectFinder ops applier")
    parser.add_argument("--watch", action="store_true", help="Run forever")
    parser.add_argument("--once", action="store_true",
                        help="Process current pending/ and publish snapshot, then exit")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SEC,
                        help="Polling interval in seconds (watch mode)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.once:
        stats = run_once()
        log(f"once: {stats}")
        return

    watch_loop(args.interval)


if __name__ == "__main__":
    main()
