#!/usr/bin/env python3
"""
ProjectFinder Telegram Notifier — local sender (SQLite edition).

Sends notifications from the `notifications` table (status='pending') to the
human operator's Telegram chat via Bot API. Cowork sandbox blocks outbound
requests to api.telegram.org, so this runs on the local machine.

Uses two-phase commit: mark_notification_sent() / mark_notification_failed()
succeed only if the row was still 'pending', making retries idempotent.

Config + bot_token come from config/notifications-config.json merged with
config/secrets.json via pf_secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, Exception):
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import pf_db          # noqa: E402
import pf_secrets     # noqa: E402


def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def send_telegram(token: str, chat_id: str, text: str,
                  outgoing_id: str | None = None,
                  notification_id: str | None = None) -> tuple[bool, dict]:
    """Send one message. If outgoing_id provided — attach inline review buttons."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if outgoing_id:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "✅ Отправить", "callback_data": f"approve:{outgoing_id}"},
                {"text": "✏️ Изменить",  "callback_data": f"edit:{outgoing_id}"},
                {"text": "❌ Отклонить",  "callback_data": f"reject:{outgoing_id}"},
            ]]
        }
    elif notification_id:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "✓ Принято", "callback_data": f"ack:{notification_id}"},
            ]]
        }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode())
            return body.get("ok", False), body
    except Exception as e:
        return False, {"error": str(e)}


def send_test(config: dict) -> None:
    bot = config.get("telegram_bot", {})
    if not bot.get("enabled"):
        log("Bot is not enabled in config.")
        return
    token = bot.get("bot_token")
    if not token or "PASTE" in token:
        log("ERROR: bot_token not set (check config/secrets.json).")
        return
    recipients = config.get("recipients", [])
    if not recipients:
        log("ERROR: no recipients configured.")
        return
    test_text = (
        "Test from local notifier.\n\n"
        "If you see this — connection is working.\n"
        f"Time: {datetime.now().isoformat()}"
    )
    for r in recipients:
        chat_id = r.get("telegram_chat_id")
        if not chat_id or "PASTE" in str(chat_id):
            log(f"Skipping {r.get('id')}: chat_id not set.")
            continue
        ok, resp = send_telegram(token, chat_id, test_text)
        if ok:
            log(f"OK: sent test to {r.get('id')} (chat_id={chat_id})")
        else:
            log(f"FAILED: {r.get('id')} — {resp}")


def send_pending(config: dict) -> int:
    """Send all pending notifications. Returns successfully-sent count."""
    bot = config.get("telegram_bot", {})
    if not bot.get("enabled"):
        return 0
    token = bot.get("bot_token")
    if not token or "PASTE" in token:
        return 0

    # Перед новым циклом поднимаем failed с истёкшим backoff обратно в pending.
    rq = pf_db.requeue_failed_notifications()
    if rq:
        log(f"retry: requeued {rq} failed notifications back to pending")

    sent_count = 0
    for n in pf_db.list_pending_notifications():
        chat_id = n.get("telegram_chat_id")
        if not chat_id or "PASTE" in str(chat_id):
            # Системный алерт без получателя (например, старый health-alert
            # c recipient=None) — retry не поможет. Сразу навсегда failed,
            # чтобы цикл не пытался доставлять заведомо неотправляемое.
            pf_db.mark_notification_failed(n["id"], "no chat_id")
            log(f"dropped notif {n['id']} — no chat_id")
            continue

        text = n.get("message_sent") or n.get("summary") or "(no message)"
        outgoing_id = n.get("outgoing_id")
        ok, resp = send_telegram(token, chat_id, text,
                                 outgoing_id=outgoing_id,
                                 notification_id=n.get("id"))
        if ok:
            message_id = resp.get("result", {}).get("message_id")
            pf_db.mark_notification_sent(
                n["id"], telegram_response={"ok": True, "message_id": message_id}
            )
            sent_count += 1
            log(f"sent notif {n['id']} ({n.get('type')})")
        else:
            # Сетевая ошибка / rate-limit — планируем retry с backoff.
            _ok, new_status = pf_db.mark_notification_failed_with_backoff(
                n["id"], json.dumps(resp, ensure_ascii=False)
            )
            log(f"FAILED notif {n['id']}: {resp} (new_status={new_status})")
    return sent_count


def watch_loop(config: dict, interval_sec: int = 60) -> None:
    log(f"Watch mode started (interval: {interval_sec}s). Ctrl+C to stop.")
    try:
        while True:
            count = send_pending(config)
            if count > 0:
                log(f"Sent {count} notifications.")
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        log("Stopped by user.")


def main() -> None:
    parser = argparse.ArgumentParser(description="ProjectFinder Telegram notifier")
    parser.add_argument("--watch", action="store_true", help="Watch mode")
    parser.add_argument("--interval", type=int, default=60,
                        help="Watch interval in seconds")
    parser.add_argument("--test", action="store_true", help="Send a test message")
    args = parser.parse_args()

    config = pf_secrets.load_config("notifications-config.json")
    pf_db.init_db()

    if args.test:
        send_test(config)
        return

    if args.watch:
        watch_loop(config, args.interval)
        return

    count = send_pending(config)
    log(f"Done. Sent {count} notifications.")


if __name__ == "__main__":
    main()
