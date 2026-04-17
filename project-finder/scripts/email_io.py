#!/usr/bin/env python3
"""
ProjectFinder Email I/O daemon (SQLite edition).

  SENDER  (SMTP):  every 30s picks up outgoing_messages with channel='email'
                   and status='ready'. Uses two-phase commit:
                       ready --(claim)--> sending --(ok)--> sent
                                                  --(err)--> failed
                   If the daemon crashes mid-send, recover_stuck_sending()
                   releases any 'sending' rows older than 5 min back to 'ready'
                   so they can be retried (or investigated).

  LISTENER (IMAP): every 60s polls INBOX for mail from any known
                   employer_contact in active email conversations. Dedup via
                   UNIQUE(channel, imap_message_id) — impossible to double-insert.

Config comes from config/email-config.json merged with secrets via pf_secrets.
Secrets (app-password) live in config/secrets.json (git-ignored).
"""

from __future__ import annotations

import argparse
import email
import imaplib
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
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

POLL_OUTGOING_SEC = 30
POLL_INBOX_SEC = 60
# 10 мин — с запасом для медленных SMTP-серверов и реконнектов TLS. Согласовано
# с telegram_io (тот же RECOVER_STUCK_AFTER_SEC=600).
RECOVER_STUCK_AFTER_SEC = 600

# Кэш профилей разработчиков: developer_id → (from_name, from_address).
# Профили меняются редко, а файлы читаются из-за каждого письма.
_DEV_IDENTITY_CACHE: dict[str, tuple[str, str]] = {}


def resolve_from_identity(smtp_cfg: dict, developer_id: str | None) -> tuple[str, str]:
    """Вернуть (from_name, from_address) для SMTP-заголовка From.

    Приоритет:
      1. developers/<id>.json → email_identity.from_name / from_address
      2. smtp_cfg.username (fallback — адрес того почтового ящика, через
         который реально идёт отправка).

    Раньше from_name/from_address хранились в email-config.json — но это
    приводило к identity-mismatch: HR видел письмо «от Алексея Морозова»,
    а тело было подписано «Иван». Теперь identity строго привязана
    к профилю разработчика.
    """
    fallback = (smtp_cfg.get("username") or "", smtp_cfg.get("username") or "")
    if not developer_id:
        return fallback
    if developer_id in _DEV_IDENTITY_CACHE:
        return _DEV_IDENTITY_CACHE[developer_id]
    dev_file = SCRIPT_DIR.parent / "config" / "developers" / f"{developer_id}.json"
    if not dev_file.exists():
        _DEV_IDENTITY_CACHE[developer_id] = fallback
        return fallback
    try:
        import json as _json
        with dev_file.open("r", encoding="utf-8") as f:
            prof = _json.load(f)
    except Exception as e:
        log(f"cannot load developer {developer_id}: {e}")
        _DEV_IDENTITY_CACHE[developer_id] = fallback
        return fallback
    # email_identity лежит ВНУТРИ блока "fixed" в developers/<id>.json
    # (см. test-fullstack.json:63). Раньше код искал на верхнем уровне →
    # всегда возвращал None → HR видел письмо «от smtp.username», подписанное
    # display_name из профиля. Identity-mismatch.
    ident = (prof.get("fixed", {}).get("email_identity")
             or prof.get("email_identity")  # back-compat, если кто-то поднял на верх
             or {})
    name = ident.get("from_name") or ""
    addr = ident.get("from_address") or fallback[1]
    result = (name, addr)
    _DEV_IDENTITY_CACHE[developer_id] = result
    return result


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [email   ] {msg}", flush=True)


# ---------- SENDER ----------

def send_via_smtp(cfg: dict, to_addr: str, subject: str, body: str,
                  developer_id: str | None = None) -> tuple[bool, str]:
    smtp_cfg = cfg["email"]["smtp"]
    from_name, from_addr = resolve_from_identity(smtp_cfg, developer_id)

    msg = MIMEMultipart()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"], timeout=30) as s:
            if smtp_cfg.get("use_tls", True):
                s.starttls()
            password = (smtp_cfg["password"] or "").replace(" ", "")
            s.login(smtp_cfg["username"], password)
            s.send_message(msg)
        return True, ""
    except Exception as e:
        return False, str(e)


def process_outgoing_emails(cfg: dict) -> None:
    """Claim one 'ready' email at a time, send it, and mark it sent/failed.

    We loop with claim_next_ready() instead of iterating a full list so that
    another process (future worker, or restart mid-loop) can't re-send a
    message we already claimed.
    """
    sent = failed = 0
    while True:
        got = pf_db.claim_next_ready("email")
        if got is None:
            break
        msg_row, attempt = got
        to = msg_row.get("recipient")
        subj = msg_row.get("subject") or "(no subject)"
        body = msg_row.get("body") or ""
        if not to:
            pf_db.mark_outgoing_failed(msg_row["id"], attempt, "no recipient")
            failed += 1
            continue
        log(f"  -> {to}: {subj!r}")
        ok, err = send_via_smtp(cfg, to, subj, body,
                                developer_id=msg_row.get("developer_id"))
        if ok:
            pf_db.mark_outgoing_sent(msg_row["id"], attempt)
            sent += 1
            log("    OK")
        else:
            # Не оставляем failed навсегда — даём backoff-retries.
            _ok, new_status = pf_db.mark_outgoing_failed_with_backoff(msg_row["id"], attempt, err)
            failed += 1
            log(f"    FAILED: {err}  (new_status={new_status})")
    if sent or failed:
        log(f"sent={sent} failed={failed}")


# ---------- LISTENER (IMAP) ----------

def get_known_email_contacts() -> dict:
    """Map lowercased sender email -> conversation dict, for active email
    conversations only."""
    result = {}
    for c in pf_db.list_conversations():
        if c.get("status") == "closed":
            continue
        if c.get("channel") != "email":
            continue
        ec = (c.get("employer_contact") or "").lower()
        if "@" in ec:
            result[ec] = c
    return result


def decode_str(s) -> str:
    if s is None:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return str(s)


def extract_text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        return payload.decode(charset, errors="replace")
                    except Exception:
                        return payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except Exception:
                return payload.decode("utf-8", errors="replace")
    return ""


def append_incoming(sender_email: str, subject: str, body: str,
                    msg_id: str, conv: dict) -> bool:
    """Insert an incoming email row. Returns True if actually inserted,
    False if it was a duplicate (UNIQUE imap_message_id)."""
    new_id = pf_db.insert_incoming({
        "id": f"in-email-{int(time.time())}-{msg_id[:12]}",
        "conversation_id": conv["id"],
        "job_id": conv.get("job_id"),
        "channel": "email",
        "sender": sender_email,
        "subject": subject,
        "text": body,
        "imap_message_id": msg_id,
        "status": "new",
    })
    if new_id:
        log(f"  <- {sender_email}: {subject!r} (conv={conv['id']})")
        return True
    return False


def _imap_since_date(conv: dict) -> str:
    """Вернуть дату для `IMAP SEARCH SINCE` в формате '01-Jan-2026'.

    Берём `conversation.created_at` минус 1 час (буфер на рассинхрон времени),
    чтобы не перетянуть историю переписки годичной давности: на первом
    запуске слушателя IMAP-поиск `FROM sender` без даты возвращал ВСЕ письма
    от этого адреса — dialogue-agent начинал отвечать на прошлогоднюю
    переписку. Фильтр по дате ограничивает окно до «после первого контакта».
    """
    from datetime import datetime as _dt, timedelta as _td
    created = conv.get("created_at") or ""
    try:
        # pf_db пишет 'YYYY-MM-DDTHH:MM:SSZ'
        dt = _dt.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        dt = _dt.utcnow() - _td(days=2)
    dt -= _td(hours=1)
    return dt.strftime("%d-%b-%Y")


def _state_key_last_uid(conv_id: str) -> str:
    return f"email_io.last_uid.{conv_id}"


def check_inbox(cfg: dict) -> None:
    imap_cfg = cfg["email"]["imap"]
    contacts = get_known_email_contacts()
    if not contacts:
        return

    try:
        with imaplib.IMAP4_SSL(imap_cfg["host"], imap_cfg["port"]) as M:
            password = (imap_cfg["password"] or "").replace(" ", "")
            M.login(imap_cfg["username"], password)
            M.select(imap_cfg.get("folder", "INBOX"))
            for sender_email, conv in contacts.items():
                since_date = _imap_since_date(conv)
                last_uid = int(pf_db.state_get(_state_key_last_uid(conv["id"]), default=0) or 0)

                # UID-based fetch: только письма с UID больше ранее виденного,
                # И по дате с момента первого контакта. Дата защищает при
                # старте (last_uid=0), UID — при рестартах процесса.
                # Канонический IMAP-синтаксис: search-keys через пробел =
                # неявный AND. Без скобок — менее придирчивые серверы (Yandex,
                # Mail.ru) тоже принимают.
                criteria = f'FROM "{sender_email}" SINCE "{since_date}"'
                if last_uid > 0:
                    criteria += f' UID {last_uid + 1}:*'
                typ, data_ids = M.uid("search", None, criteria)
                if typ != "OK":
                    continue

                max_uid_seen = last_uid
                for raw_uid in data_ids[0].split():
                    uid = raw_uid.decode()
                    try:
                        uid_int = int(uid)
                    except ValueError:
                        continue

                    typ, data_msg = M.uid("fetch", uid, "(RFC822)")
                    if typ != "OK" or not data_msg or not data_msg[0]:
                        continue
                    raw = data_msg[0][1]
                    msg = email.message_from_bytes(raw)
                    msg_id = msg.get("Message-ID") or f"no-id-uid-{uid}"
                    subj = decode_str(msg.get("Subject"))
                    from_hdr = decode_str(msg.get("From"))
                    _, addr = parseaddr(from_hdr)
                    body = extract_text_body(msg)

                    inserted = append_incoming(addr.lower(), subj, body, msg_id, conv)
                    if inserted:
                        # Пометим письмо как \Seen, чтобы IMAP-клиенты
                        # пользователя тоже видели «обработано»; и чтобы на
                        # случай миграции на UNSEEN-фильтр — не видеть его снова.
                        try:
                            M.uid("store", uid, "+FLAGS", "\\Seen")
                        except Exception as e:
                            log(f"  cannot \\Seen uid={uid}: {e}")

                    if uid_int > max_uid_seen:
                        max_uid_seen = uid_int

                if max_uid_seen > last_uid:
                    pf_db.state_set(_state_key_last_uid(conv["id"]), max_uid_seen)
    except Exception as e:
        log(f"IMAP error: {e}")


# ---------- MAIN ----------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    cfg = pf_secrets.load_config("email-config.json")
    if not cfg.get("email", {}).get("enabled"):
        log("email disabled in config (set email.enabled=true); exiting")
        return
    smtp_pw = cfg["email"]["smtp"].get("password", "")
    if not smtp_pw or "PASTE" in smtp_pw:
        log("ERROR: SMTP app-password not set (check config/secrets.json); exiting")
        return

    pf_db.init_db()
    # Any messages marked 'sending' by a previous crashed run go back to ready.
    n = pf_db.recover_stuck_sending(RECOVER_STUCK_AFTER_SEC)
    if n:
        log(f"recovered {n} stuck 'sending' row(s) back to 'ready'")

    log("email I/O started (SMTP send + IMAP listen)")
    last_inbox_check = 0
    last_recovery_check = time.time()
    try:
        while True:
            # Сначала поднимаем failed с истёкшим backoff обратно в ready —
            # retry-политика работает в каждом тике, не только при старте.
            rq = pf_db.requeue_failed_for_retry()
            if rq:
                log(f"retry: requeued {rq} failed outgoing back to 'ready'")
            process_outgoing_emails(cfg)
            now = time.time()
            if now - last_inbox_check >= POLL_INBOX_SEC:
                check_inbox(cfg)
                last_inbox_check = now
            # Periodic recovery sweep in case we ran for a long time and some
            # worker froze.
            if now - last_recovery_check >= RECOVER_STUCK_AFTER_SEC:
                m = pf_db.recover_stuck_sending(RECOVER_STUCK_AFTER_SEC)
                if m:
                    log(f"periodic recovery: {m} row(s) re-queued")
                last_recovery_check = now
            if args.once:
                break
            time.sleep(POLL_OUTGOING_SEC)
    except KeyboardInterrupt:
        log("stopped by user")


if __name__ == "__main__":
    main()
