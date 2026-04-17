#!/usr/bin/env python3
"""
ProjectFinder — единый launcher для всех фоновых сервисов.

Запусти эту команду один раз:
    py -3 project-finder/scripts/projectfinder.py

Поднимает шесть сервисов в параллельных тредах:
  1. ops_applier.py        — применяет intent-файлы от Cowork к БД,
                             публикует data/snapshot.sqlite для Cowork-чтения.
  2. telegram_scanner.py   — сканирует TG-каналы через Telethon, пишет jobs.
  3. telegram_notifier.py  — шлёт notifications(pending) в Telegram-бота.
  4. telegram_io.py        — Telethon: входящие DM + отправка outgoing(ready).
  5. bot_handler.py        — callback-кнопки бота (approve / edit / reject).
  6. email_io.py           — SMTP-отправка + IMAP-поллинг входящей почты.

Ctrl+C — корректная остановка всех сервисов.
При сбое любого из сервисов — автоматический перезапуск через 30 секунд.
Все логи объединены в один поток + сохраняются в logs/projectfinder.log.
"""

import argparse
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, Exception):
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "projectfinder.log"

# Лок для синхронной записи логов из разных тредов
log_lock = threading.Lock()
shutdown_requested = False

SERVICES = [
    {
        # Шестой сервис. Единственный «писатель» в БД со стороны Cowork-скиллов:
        # читает intent-файлы из data/intents/pending/, применяет через pf_db.*,
        # раз в минуту публикует data/snapshot.sqlite для Cowork-чтения.
        # Подробности — scripts/ops_applier.py.
        "name": "ops_app",
        "script": "ops_applier.py",
        "args": ["--watch", "--interval", "5"],
        "color": "\033[91m",  # red
    },
    {
        "name": "scanner",
        "script": "telegram_scanner.py",
        "args": ["--watch", "--interval", "30"],
        "color": "\033[94m",  # blue
    },
    {
        "name": "notifier",
        "script": "telegram_notifier.py",
        "args": ["--watch", "--interval", "60"],
        "color": "\033[92m",  # green
    },
    {
        "name": "tg_io",
        "script": "telegram_io.py",
        "args": ["--watch"],
        "color": "\033[95m",  # magenta
    },
    {
        "name": "bot_hndl",
        "script": "bot_handler.py",
        "args": [],
        "color": "\033[93m",  # yellow
    },
    {
        "name": "email_io",
        "script": "email_io.py",
        "args": ["--watch"],
        "color": "\033[96m",  # cyan
    },
]
RESET = "\033[0m"


def log(name: str, line: str, color: str = "") -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{timestamp}] [{name:8}] {line.rstrip()}"
    with log_lock:
        # Console with color
        print(f"{color}{formatted}{RESET}", flush=True)
        # File without color codes
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(formatted + "\n")


def stream_output(proc: subprocess.Popen, name: str, color: str) -> None:
    """Read service stdout and forward each line to the unified log."""
    try:
        for line in iter(proc.stdout.readline, ""):
            if line:
                log(name, line, color)
            if shutdown_requested:
                break
    except Exception as e:
        log(name, f"stream error: {e}", color)


def run_service(service: dict) -> None:
    """Run one service in a loop with auto-restart on crash."""
    name = service["name"]
    script = service["script"]
    args = service["args"]
    color = service["color"]
    script_path = SCRIPT_DIR / script

    if not script_path.exists():
        log(name, f"ERROR: {script_path} not found", color)
        return

    while not shutdown_requested:
        log(name, f"starting: {script} {' '.join(args)}", color)
        try:
            import os
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"  # force UTF-8 in child processes
            env["PYTHONUTF8"] = "1"
            proc = subprocess.Popen(
                [sys.executable, str(script_path), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except Exception as e:
            log(name, f"failed to start: {e}", color)
            time.sleep(30)
            continue

        # Stream output until process ends or shutdown
        stream_output(proc, name, color)
        proc.wait()

        if shutdown_requested:
            log(name, "stopping (shutdown requested)", color)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            return

        exit_code = proc.returncode
        log(name, f"exited with code {exit_code}. Restart in 30s...", color)
        for _ in range(30):
            if shutdown_requested:
                return
            time.sleep(1)


def handle_shutdown(signum, frame) -> None:
    global shutdown_requested
    shutdown_requested = True
    print("\n[launcher] Shutdown requested. Stopping services...", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="ProjectFinder unified launcher")
    parser.add_argument(
        "--only",
        choices=[s["name"] for s in SERVICES],
        help="Run only one named service (for debugging)",
    )
    args = parser.parse_args()

    services_to_run = SERVICES
    if args.only:
        services_to_run = [s for s in SERVICES if s["name"] == args.only]

    # Graceful shutdown on Ctrl+C
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    log("launcher", "=" * 60)
    log("launcher", "ProjectFinder launcher started")
    log("launcher", f"Services: {[s['name'] for s in services_to_run]}")
    log("launcher", f"Logs: {LOG_FILE}")
    log("launcher", "Press Ctrl+C to stop all services gracefully")
    log("launcher", "=" * 60)

    # Start each service in its own thread (so they run in parallel,
    # auto-restart, and stream output to the same console)
    threads = []
    for svc in services_to_run:
        t = threading.Thread(target=run_service, args=(svc,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(1)  # stagger starts

    # Wait for shutdown signal
    try:
        while not shutdown_requested:
            time.sleep(1)
    except KeyboardInterrupt:
        handle_shutdown(None, None)

    # Give threads time to clean up
    for t in threads:
        t.join(timeout=10)

    log("launcher", "All services stopped. Bye.")


if __name__ == "__main__":
    main()
