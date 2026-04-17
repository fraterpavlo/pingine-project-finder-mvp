#!/usr/bin/env python3
"""
ProjectFinder — полный ресет БД и рабочих файлов.

Что делает:
1. Запрашивает явное подтверждение (если не передан флаг --yes).
2. Удаляет строки из ВСЕХ runtime-таблиц (схема остаётся).
   Затрагиваемые таблицы:
       jobs, conversations, conversation_messages,
       incoming_messages, outgoing_messages,
       notifications, escalations, service_state, seen_message_ids
3. Чинит stale WAL/SHM/lock файлы рядом с projectfinder.sqlite.
4. Очищает папки data/drafts/ и data/reports/ (если существуют).
5. Оставляет нетронутыми:
       - config/*        (все настройки)
       - config/secrets.json
       - config/developers/*.json
       - scripts/*.py
       - логи и любые другие файлы ВНЕ data/

ВАЖНО: перед запуском ОСТАНОВИ launcher (projectfinder.py) и убедись, что
ни один из демонов (scanner/notifier/tg_io/bot/email_io) не работает —
иначе WAL/SHM могут снова зависнуть.

Использование:
    python3 project-finder/scripts/reset_db.py           # интерактивно
    python3 project-finder/scripts/reset_db.py --yes     # без подтверждения
    python3 project-finder/scripts/reset_db.py --dry-run # только показать что будет
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "projectfinder.sqlite"

# Порядок важен: conversation_messages ссылается на conversations (FK ON).
TABLES_TO_TRUNCATE = [
    "conversation_messages",
    "incoming_messages",
    "outgoing_messages",
    "escalations",
    "notifications",
    "conversations",
    "jobs",
    "service_state",
    "seen_message_ids",
]

STALE_SIDECAR_PATTERNS = [
    "projectfinder.sqlite-wal",
    "projectfinder.sqlite-shm",
]

SUBDIRS_TO_CLEAR = ["drafts", "reports"]


def say(msg: str) -> None:
    print(msg, flush=True)


def count_rows(conn: sqlite3.Connection, tbl: str) -> int:
    try:
        return conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
    except sqlite3.DatabaseError:
        return -1


def plan_report() -> list[str]:
    lines = []
    if not DB_PATH.exists():
        lines.append(f"БД {DB_PATH} не существует — будет создана пустая.")
        return lines
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        for tbl in TABLES_TO_TRUNCATE:
            n = count_rows(conn, tbl)
            if n < 0:
                lines.append(f"  {tbl}: (не читается — таблица повреждена или отсутствует)")
            elif n > 0:
                lines.append(f"  {tbl}: {n} строк → будет удалено")
            else:
                lines.append(f"  {tbl}: уже пусто")
        conn.close()
    except sqlite3.DatabaseError as e:
        lines.append(f"(не смог открыть БД для подсчёта: {e})")

    sidecar_found = []
    for p in STALE_SIDECAR_PATTERNS:
        fp = DATA_DIR / p
        if fp.exists():
            sidecar_found.append(str(fp))
    for p in DATA_DIR.glob(".fuse_hidden*"):
        sidecar_found.append(str(p))
    lock = DATA_DIR / "projectfinder.sqlite.lock"
    if lock.exists():
        sidecar_found.append(str(lock) + (" (dir)" if lock.is_dir() else " (file)"))
    if sidecar_found:
        lines.append("")
        lines.append("Будут удалены sidecar-файлы:")
        for f in sidecar_found:
            lines.append(f"  - {f}")

    cleared_dirs = []
    for sub in SUBDIRS_TO_CLEAR:
        d = DATA_DIR / sub
        if d.exists() and any(d.iterdir()):
            count = sum(1 for _ in d.iterdir())
            cleared_dirs.append((d, count))
    if cleared_dirs:
        lines.append("")
        lines.append("Содержимое папок будет удалено:")
        for d, n in cleared_dirs:
            lines.append(f"  - {d}: {n} записей")

    return lines


def truncate_tables(conn: sqlite3.Connection) -> None:
    # Временно отключаем FK — чтобы порядок удаления не имел значения для exotic cases.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for tbl in TABLES_TO_TRUNCATE:
            try:
                conn.execute(f"DELETE FROM {tbl}")
                say(f"  DELETE FROM {tbl} — ok")
            except sqlite3.DatabaseError as e:
                say(f"  DELETE FROM {tbl} — пропущено ({e})")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("VACUUM")
        say("  VACUUM — ok (файл БД сжат)")
    except sqlite3.DatabaseError as e:
        say(f"  VACUUM — пропущено ({e})")


def remove_sidecars() -> None:
    for p in STALE_SIDECAR_PATTERNS:
        fp = DATA_DIR / p
        if fp.exists():
            try:
                fp.unlink()
                say(f"  удалён {fp.name}")
            except OSError as e:
                say(f"  НЕ смог удалить {fp.name}: {e}")
    for p in DATA_DIR.glob(".fuse_hidden*"):
        try:
            p.unlink()
            say(f"  удалён {p.name}")
        except OSError as e:
            say(f"  НЕ смог удалить {p.name}: {e}")
    lock = DATA_DIR / "projectfinder.sqlite.lock"
    if lock.exists():
        try:
            if lock.is_dir():
                shutil.rmtree(lock)
            else:
                lock.unlink()
            say(f"  удалён {lock.name}")
        except OSError as e:
            say(f"  НЕ смог удалить {lock.name}: {e}")


def clear_subdirs() -> None:
    for sub in SUBDIRS_TO_CLEAR:
        d = DATA_DIR / sub
        if not d.exists():
            continue
        removed = 0
        for item in d.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                removed += 1
            except OSError as e:
                say(f"  {item}: не удалён — {e}")
        say(f"  папка {sub}/: удалено {removed} записей")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="без подтверждения")
    ap.add_argument("--dry-run", action="store_true", help="только показать план")
    args = ap.parse_args()

    say("=" * 60)
    say("ProjectFinder — полный ресет БД и рабочих файлов")
    say("=" * 60)
    say(f"DB_PATH = {DB_PATH}")
    say("")
    say("Перед запуском убедись, что:")
    say("  1) launcher (projectfinder.py) остановлен")
    say("  2) все демоны (scanner/notifier/tg_io/bot/email_io) не работают")
    say("  3) никто не держит файл БД открытым")
    say("")
    say("План:")
    for line in plan_report():
        say(line)
    say("")

    if args.dry_run:
        say("--dry-run: реальных изменений не делаем. Завершаю.")
        return 0

    if not args.yes:
        ans = input("Точно сбросить всё это? (yes/NO): ").strip().lower()
        if ans != "yes":
            say("Отменено.")
            return 1

    say("")
    say("→ Работаем с БД:")
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=15.0, isolation_level=None)
            conn.execute("PRAGMA busy_timeout=15000")
            truncate_tables(conn)
            conn.close()
        except sqlite3.DatabaseError as e:
            say(f"  БД не открывается / повреждена ({e}).")
            say(f"  Рекомендую переместить файл БД вручную (mv {DB_PATH} {DB_PATH}.broken)")
            say(f"  и запустить скрипт повторно — он пересоздаст чистую БД.")
            return 2
    else:
        say("  БД не существует, пропускаю truncate.")

    say("")
    say("→ Чистим sidecar-файлы:")
    remove_sidecars()

    say("")
    say("→ Чистим рабочие подпапки:")
    clear_subdirs()

    say("")
    say("Готово. Теперь можно запустить launcher — pf_db.init_db() пересоздаст")
    say("нужные таблицы (если их нет), и инструмент стартует как первый раз.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
