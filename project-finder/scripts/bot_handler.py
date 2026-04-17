#!/usr/bin/env python3
"""
ProjectFinder bot handler (SQLite edition).

Слушает callback_query (нажатия inline-кнопок) и текстовые ответы пользователя
в чате с ботом. Позволяет:

  ✅ Approve  →  outgoing_messages.status: 'needs_review' → 'ready'
                 (telegram_io / email_io подхватит и отправит)
  ✏️ Edit    →  бот просит ответить новым текстом → обновляет body и одобряет
  ❌ Reject   →  status = 'rejected'
  ✓ Ack      →  notifications.acknowledged = 1

State (last_update_id, awaiting_edit) хранится в service_state через pf_db.

Bot token берётся из config/notifications-config.json + config/secrets.json.
"""

from __future__ import annotations

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

POLL_INTERVAL = 3  # seconds between retries on error
STATE_KEY = "bot_handler_state"   # single key in service_state
AWAITING_EDIT_TTL_SEC = 10 * 60   # 10 минут на правку; иначе просроченный
                                   # «я хочу изменить» превращает любой твой
                                   # следующий текст в редактирование старого
                                   # черновика. TTL гасит это поведение.


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [bot     ] {msg}", flush=True)


# ---------- state (last_update_id, awaiting_edit) ----------

def _default_state() -> dict:
    return {"last_update_id": 0, "awaiting_edit": {}}


def load_state() -> dict:
    st = pf_db.state_get(STATE_KEY, default=_default_state())
    if not isinstance(st, dict):
        return _default_state()
    # Always ensure keys exist.
    st.setdefault("last_update_id", 0)
    st.setdefault("awaiting_edit", {})
    return st


def save_state(state: dict) -> None:
    pf_db.state_set(STATE_KEY, state)


# ---------- Telegram API ----------

def tg_api(token: str, method: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=35) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def answer_callback(token: str, callback_id: str, text: str = "") -> None:
    tg_api(token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def send_msg(token: str, chat_id: str, text: str,
             reply_markup: dict | None = None) -> dict:
    p = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        p["reply_markup"] = reply_markup
    return tg_api(token, "sendMessage", p)


def edit_msg_buttons(token: str, chat_id: str, message_id: int, text: str) -> None:
    tg_api(token, "editMessageText", {
        "chat_id": chat_id, "message_id": message_id, "text": text,
        "disable_web_page_preview": True,
    })


# ---------- Action handlers ----------

def handle_approve(token: str, chat_id: str, msg_id: int, out_id: str) -> None:
    ok = pf_db.approve_outgoing(out_id)
    msg = pf_db.get_outgoing(out_id)
    if not msg:
        send_msg(token, chat_id, f"⚠️ Не нашёл outgoing {out_id}")
        return
    if not ok:
        # Not in needs_review anymore — maybe already approved or sent. Be polite.
        edit_msg_buttons(token, chat_id, msg_id,
                         f"ℹ️ Уже обработано (status={msg.get('status')})")
        return
    edit_msg_buttons(token, chat_id, msg_id,
                     f"✅ ОДОБРЕНО — будет отправлено в течение 30с\n\n"
                     f"Получатель: {msg.get('recipient')}\n"
                     f"Канал: {msg.get('channel')}\n\n"
                     f"Текст:\n{msg.get('body')}")
    log(f"approved {out_id}")


def handle_reject(token: str, chat_id: str, msg_id: int, out_id: str) -> None:
    ok = pf_db.reject_outgoing(out_id)
    msg = pf_db.get_outgoing(out_id)
    if not msg:
        send_msg(token, chat_id, f"⚠️ Не нашёл outgoing {out_id}")
        return
    if not ok:
        edit_msg_buttons(token, chat_id, msg_id,
                         f"ℹ️ Уже обработано (status={msg.get('status')})")
        return
    edit_msg_buttons(token, chat_id, msg_id,
                     f"❌ ОТКЛОНЕНО — отправка отменена\n\n"
                     f"Получатель: {msg.get('recipient')}")
    log(f"rejected {out_id}")


def handle_edit_request(token: str, chat_id: str, msg_id: int, out_id: str,
                        state: dict) -> None:
    """Remember which outgoing this user is editing; their next text replaces body."""
    msg = pf_db.get_outgoing(out_id)
    if not msg:
        send_msg(token, chat_id, f"⚠️ Не нашёл outgoing {out_id}")
        return
    state.setdefault("awaiting_edit", {})[str(chat_id)] = {
        "outgoing_id": out_id,
        "notif_message_id": msg_id,
        "expires_at": int(time.time()) + AWAITING_EDIT_TTL_SEC,
    }
    save_state(state)
    send_msg(token, chat_id,
             f"✏️ Отправь следующим сообщением НОВЫЙ текст для:\n\n"
             f"Получатель: {msg.get('recipient')}\n\n"
             f"Текущий черновик:\n{msg.get('body')}\n\n"
             f"Просто напиши ответ — я заменю им текст и одобрю отправку.")
    log(f"edit requested for {out_id}")


def handle_user_text(token: str, chat_id: str, text: str, state: dict) -> None:
    awaiting = state.get("awaiting_edit", {}).get(str(chat_id))
    if not awaiting:
        send_msg(token, chat_id,
                 "ℹ️ Я слушаю команды через кнопки в уведомлениях.\n"
                 "Если ты хотел что-то отредактировать — нажми ✏️ в нужном уведомлении.")
        return
    # TTL: «✏️» без последующего текста в течение 10 минут не должно
    # превращать случайный разговор в правку старого черновика.
    expires_at = awaiting.get("expires_at")
    if isinstance(expires_at, int) and time.time() > expires_at:
        state["awaiting_edit"].pop(str(chat_id), None)
        save_state(state)
        send_msg(token, chat_id,
                 "⏱ Предыдущая заявка на правку просрочена (>10 мин). "
                 "Если всё ещё хочешь что-то поменять — нажми ✏️ в нужном уведомлении ещё раз.")
        return
    out_id = awaiting["outgoing_id"]
    notif_msg_id = awaiting["notif_message_id"]

    msg = pf_db.get_outgoing(out_id)
    if not msg:
        send_msg(token, chat_id, f"⚠️ Не нашёл outgoing {out_id} (возможно удалён)")
    else:
        # Approve + replace body in one atomic UPDATE (uses approve_outgoing's
        # WHERE status='needs_review' guard). If it's no longer needs_review,
        # we fall back to updating body directly.
        approved = pf_db.approve_outgoing(out_id, edited_body=text, edited_by="user")
        if not approved:
            pf_db.update_outgoing_body(out_id, text, edited_by="user")
        send_msg(token, chat_id,
                 f"✅ Текст обновлён и одобрен. Будет отправлено в течение 30с.\n\n"
                 f"Новый текст:\n{text}")
        try:
            edit_msg_buttons(token, chat_id, notif_msg_id,
                             f"✏️ ОТРЕДАКТИРОВАНО ВРУЧНУЮ — будет отправлено\n\n"
                             f"Получатель: {msg.get('recipient')}\n\n"
                             f"Новый текст:\n{text}")
        except Exception:
            pass
    state["awaiting_edit"].pop(str(chat_id), None)
    save_state(state)
    log(f"edited {out_id} with new body")


def handle_review_command(token: str, chat_id: str) -> None:
    pending = pf_db.list_needs_review()
    if not pending:
        send_msg(token, chat_id, "✅ Нет черновиков, ждущих ревью.")
        return
    send_msg(token, chat_id, f"📝 Ждут ревью: {len(pending)}")
    for m in pending:
        text = (
            f"✉️ {m.get('channel', '?')} → {m.get('recipient', '?')}\n\n"
            f"{m.get('body', '(пустой текст)')}"
        )
        if len(text) > 3500:
            text = text[:3500] + "\n\n…(обрезано)"
        reply_markup = {
            "inline_keyboard": [[
                {"text": "✅ Отправить", "callback_data": f"approve:{m['id']}"},
                {"text": "✏️ Изменить", "callback_data": f"edit:{m['id']}"},
                {"text": "❌ Отклонить", "callback_data": f"reject:{m['id']}"},
            ]]
        }
        send_msg(token, chat_id, text, reply_markup=reply_markup)
    log(f"sent {len(pending)} review items to chat {chat_id}")


def handle_status_command(token: str, chat_id: str) -> None:
    c = pf_db.counts()
    text = (
        "📊 Status\n\n"
        "Outgoing:\n"
        f"  ready: {c['outgoing_ready']}\n"
        f"  sending: {c['outgoing_sending']}\n"
        f"  needs_review: {c['outgoing_needs_review']}\n"
        f"  sent: {c['outgoing_sent']}\n"
        f"  failed: {c['outgoing_failed']}\n\n"
        "Incoming:\n"
        f"  new: {c['incoming_new']}\n\n"
        f"Conversations active: {c['conv_active']}\n"
        f"Notifications pending: {c['notifications_pending']}\n"
        f"Escalations open: {c['escalations_open']}\n"
        f"Jobs (matched/total): {c['jobs_matched']}/{c['jobs']}"
    )
    send_msg(token, chat_id, text)


def handle_ack(token: str, chat_id: str, msg_id: int, notif_id: str) -> None:
    ok = pf_db.ack_notification(notif_id)
    if ok:
        edit_msg_buttons(token, chat_id, msg_id, "✓ Принято к сведению")
        log(f"acknowledged {notif_id}")


# ---------- Main loop ----------

def main_loop() -> None:
    pf_db.init_db()
    config = pf_secrets.load_config("notifications-config.json")
    bot = config.get("telegram_bot", {})
    if not bot.get("enabled"):
        log("bot disabled in config; exiting")
        return
    token = bot.get("bot_token")
    if not token or "PASTE" in token:
        log("bot_token not configured (check config/secrets.json); exiting")
        return

    state = load_state()
    log("bot handler started; polling getUpdates...")

    while True:
        try:
            resp = tg_api(token, "getUpdates", {
                "offset": int(state.get("last_update_id", 0)) + 1,
                "timeout": 25,  # long polling
                "allowed_updates": ["message", "callback_query"],
            })
            if not resp.get("ok"):
                log(f"getUpdates error: {resp}")
                time.sleep(POLL_INTERVAL)
                continue
            for upd in resp.get("result", []):
                state["last_update_id"] = upd["update_id"]

                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    cb_id = cq["id"]
                    chat_id = str(cq["message"]["chat"]["id"])
                    msg_id = cq["message"]["message_id"]
                    payload = cq.get("data", "")
                    action, _, target = payload.partition(":")
                    answer_callback(token, cb_id, "")
                    if action == "approve":
                        handle_approve(token, chat_id, msg_id, target)
                    elif action == "reject":
                        handle_reject(token, chat_id, msg_id, target)
                    elif action == "edit":
                        handle_edit_request(token, chat_id, msg_id, target, state)
                    elif action == "ack":
                        handle_ack(token, chat_id, msg_id, target)

                elif "message" in upd and upd["message"].get("text"):
                    chat_id = str(upd["message"]["chat"]["id"])
                    text = upd["message"]["text"]
                    if text.startswith("/start"):
                        send_msg(token, chat_id,
                                 "Привет! Я ProjectFinder бот.\n\n"
                                 "Команды:\n"
                                 "/review — показать черновики, ждущие ревью\n"
                                 "/status — сводка по очередям\n\n"
                                 "Иначе я сам присылаю уведомления, когда агент готовит ответ HR.")
                    elif text.startswith("/review"):
                        handle_review_command(token, chat_id)
                    elif text.startswith("/status"):
                        handle_status_command(token, chat_id)
                    else:
                        handle_user_text(token, chat_id, text, state)

                # Persist after each update so we don't replay on crash.
                save_state(state)
        except KeyboardInterrupt:
            log("stopped by user")
            break
        except Exception as e:
            log(f"loop error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main_loop()
