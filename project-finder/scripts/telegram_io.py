#!/usr/bin/env python3
"""
ProjectFinder Telegram I/O daemon (SQLite edition).

Two responsibilities run together in watch mode:

  1. SENDER: every POLL_INTERVAL_SEC seconds claims the oldest 'ready' Telegram
     outgoing_message via pf_db.claim_next_ready("telegram"), sends it via
     Telethon, then marks it 'sent' or 'failed'. Two-phase commit prevents
     double-send on restart.

  2. LISTENER: subscribes to incoming DMs; matches sender to any active
     conversation's employer_contact and inserts one row into incoming_messages.
     Dedup via UNIQUE(channel,tg_message_id,sender) makes duplicate inserts
     impossible.

Safety:
  - Rate limit: at least MIN_DELAY_BETWEEN_SENDS between any two messages;
    at least MIN_DELAY_PER_RECIPIENT between two messages to the same user.
  - Human-like random delay of HUMAN_DELAY_RANGE before sending REPLIES.

Config / secrets come from config/telegram-client-config.json merged with
config/secrets.json via pf_secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, Exception):
    pass

try:
    from telethon import TelegramClient, events
    from telethon.errors import FloodWaitError, UsernameNotOccupiedError
except ImportError:
    print("ERROR: telethon not installed. Run: pip install telethon")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import pf_db          # noqa: E402
import pf_secrets     # noqa: E402

POLL_INTERVAL_SEC = 30
MIN_DELAY_BETWEEN_SENDS = 30
MIN_DELAY_PER_RECIPIENT = 60
HUMAN_DELAY_RANGE = (30, 180)
# RECOVER_STUCK_AFTER_SEC должен быть с запасом БОЛЬШЕ HUMAN_DELAY_RANGE[1] +
# MIN_DELAY_BETWEEN_SENDS + typing-эмуляция (~15с) + сетевая латентность.
# Раньше было 300с — впритык к 180+30+15=225, и при первом же глюке сети
# сообщение могло быть отправлено дважды (recover вернул в ready, claim'нул
# второй процесс/итерация → дубль HR). 600с даёт надёжный буфер.
RECOVER_STUCK_AFTER_SEC = 600


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [tg_io  ] {msg}", flush=True)


def get_known_contacts() -> set:
    """Set of @username values from active telegram conversations."""
    contacts = set()
    for c in pf_db.list_conversations():
        if c.get("status") == "closed":
            continue
        if c.get("channel") != "telegram":
            continue
        ec = (c.get("employer_contact") or "")
        if ec.startswith("@"):
            contacts.add(ec.lower())
    return contacts


# ---------- SENDER ----------

_last_send_time = 0.0
_last_send_per_recipient: dict = {}


async def process_outgoing(client: TelegramClient) -> None:
    """Claim 'ready' telegram messages one by one, honoring rate limits.

    We peek at a small batch of ready messages first (without claiming) so we
    can enforce rate limits before taking ownership. Only after limits pass do
    we call claim_outgoing_for_sending() and actually send.

    Rate-limit semantics:
      - Global limit (min delay between ANY two sends) → no point continuing
        this cycle; we `return`.
      - Per-recipient limit → THIS candidate has to wait, but the next
        candidate addressed to a different recipient may be sendable now.
        So we `continue` to the next candidate, not `return`. (Before the fix,
        two messages to the same handler in a row blocked everyone else for
        a full cycle.)
    """
    global _last_send_time
    sent_count = failed_count = 0
    # Локальный набор claimed id — чтобы при `continue` не подбирать тот же
    # кандидат из повторного `list_outgoing_by_status`.
    skipped_ids: set[str] = set()

    while True:
        # Peek at up to 10 candidates at a time — per-recipient лимит всё
        # равно может заставить пропустить кого-то.
        candidates = pf_db.list_outgoing_by_status("ready", channel="telegram", limit=10)
        # Отфильтруем те, что в этом цикле уже пропустили из-за per-recipient
        # лимита — на следующем тике они всё ещё 'ready', но нет смысла
        # перебирать их снова.
        candidates = [c for c in candidates if c["id"] not in skipped_ids]
        if not candidates:
            break

        candidate = candidates[0]
        recipient = candidate.get("recipient", "")
        if not recipient:
            # No recipient — mark failed directly (would never succeed).
            attempt = pf_db.claim_outgoing_for_sending(candidate["id"])
            if attempt:
                pf_db.mark_outgoing_failed(candidate["id"], attempt, "no recipient")
                failed_count += 1
            continue

        now = time.time()
        # Global rate limit — действительно нет смысла продолжать сейчас.
        if now - _last_send_time < MIN_DELAY_BETWEEN_SENDS:
            return
        # Per-recipient rate limit — ЭТОТ адресат подождёт, следующий кандидат
        # может быть к другому. `continue`, а не `return`.
        last_to = _last_send_per_recipient.get(recipient, 0)
        if now - last_to < MIN_DELAY_PER_RECIPIENT:
            skipped_ids.add(candidate["id"])
            continue

        # Claim it atomically — if someone else already took it (unlikely, but
        # possible if there are two senders running), move on.
        attempt = pf_db.claim_outgoing_for_sending(candidate["id"])
        if attempt is None:
            continue

        # Human-like delay for replies only.
        if candidate.get("is_reply"):
            delay = random.randint(*HUMAN_DELAY_RANGE)
            log(f"  delaying reply to {recipient} for {delay}s (human-like)")
            await asyncio.sleep(delay)

        try:
            log(f"  -> {recipient}: {candidate['body'][:60]!r}...")
            entity = await client.get_entity(recipient.lstrip("@"))

            body_len = len(candidate["body"])
            typing_sec = min(3 + body_len / 40, 15)
            try:
                async with client.action(entity, "typing"):
                    await asyncio.sleep(typing_sec)
            except Exception:
                pass

            res = await client.send_message(entity, candidate["body"])
            channel_message_id = str(getattr(res, "id", "") or "")
            pf_db.mark_outgoing_sent(candidate["id"], attempt, channel_message_id or None)
            sent_count += 1
            _last_send_time = time.time()
            _last_send_per_recipient[recipient] = _last_send_time
        except UsernameNotOccupiedError:
            # Невалидный username — retry не поможет, сразу failed без backoff.
            pf_db.mark_outgoing_failed(candidate["id"], attempt, f"username {recipient} not found")
            failed_count += 1
            log(f"    FAILED: {recipient} not found")
        except FloodWaitError as e:
            # Telegram сам сказал подождать — releasesing claim через backoff-API,
            # чтобы в retry_count/next_retry_at попали реальные значения.
            _ok, new_status = pf_db.mark_outgoing_failed_with_backoff(
                candidate["id"], attempt, f"flood_wait={e.seconds}s"
            )
            log(f"    FLOOD WAIT {e.seconds}s — pausing (new_status={new_status})")
            await asyncio.sleep(min(e.seconds, 120))
            return
        except Exception as e:
            # Сетевые сбои и прочие временные ошибки — тоже retry.
            pf_db.mark_outgoing_failed_with_backoff(candidate["id"], attempt, str(e))
            failed_count += 1
            log(f"    FAILED: {e}")

    if sent_count or failed_count:
        log(f"sent={sent_count} failed={failed_count}")


# ---------- LISTENER ----------

def find_conversation(sender_username: str) -> dict | None:
    target = sender_username.lower()
    for c in pf_db.list_conversations():
        if c.get("status") == "closed":
            continue
        if c.get("channel") != "telegram":
            continue
        ec = (c.get("employer_contact") or "").lstrip("@").lower()
        if ec == target:
            return c
    return None


def append_incoming(sender_username: str, text: str, msg_id: int) -> None:
    conv = find_conversation(sender_username)
    if not conv:
        log(f"  ignored DM from @{sender_username} (no active conversation)")
        return
    new_id = pf_db.insert_incoming({
        "id": f"in-tg-{int(time.time())}-{msg_id}",
        "conversation_id": conv["id"],
        "job_id": conv.get("job_id"),
        "channel": "telegram",
        "sender": f"@{sender_username}",
        "text": text,
        "tg_message_id": str(msg_id),
        "status": "new",
    })
    if new_id:
        log(f"  <- @{sender_username}: {text[:60]!r}... (conv={conv['id']})")
    # if dedup kicked in (UNIQUE), new_id is None — silently skipped.


# ---------- MAIN LOOP ----------

async def main_async(once: bool) -> None:
    config = pf_secrets.load_config("telegram-client-config.json")
    tg = config.get("telegram_client", {})
    if not tg.get("enabled"):
        log("telegram_client disabled in config; exiting")
        return
    api_id = tg["api_id"]
    api_hash = tg["api_hash"]
    session_name = tg.get("session_name", "projectfinder")
    session_path = SCRIPT_DIR / session_name

    pf_db.init_db()
    n = pf_db.recover_stuck_sending(RECOVER_STUCK_AFTER_SEC)
    if n:
        log(f"recovered {n} stuck 'sending' row(s) back to 'ready'")

    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.start()
    log(f"connected as Telegram user {(await client.get_me()).username or 'self'}")

    @client.on(events.NewMessage(incoming=True))
    async def on_dm(event):
        if not event.is_private:
            return
        sender = await event.get_sender()
        username = (sender.username or "").lower() if sender else ""
        if not username:
            return
        known = get_known_contacts()
        if f"@{username}" not in known:
            return
        text = event.message.text or event.message.message or ""
        append_incoming(username, text, event.message.id)

    log("listener active. Polling outgoing every 30s. Ctrl+C to stop.")

    last_recovery = time.time()
    try:
        while True:
            # Retry-политика: failed с истёкшим backoff обратно в 'ready'.
            rq = pf_db.requeue_failed_for_retry()
            if rq:
                log(f"retry: requeued {rq} failed outgoing back to 'ready'")
            await process_outgoing(client)
            if once:
                break
            now = time.time()
            if now - last_recovery >= RECOVER_STUCK_AFTER_SEC:
                m = pf_db.recover_stuck_sending(RECOVER_STUCK_AFTER_SEC)
                if m:
                    log(f"periodic recovery: {m} row(s) re-queued")
                last_recovery = now
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except KeyboardInterrupt:
        log("stopped by user")
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="ProjectFinder TG I/O daemon")
    parser.add_argument("--once", action="store_true",
                        help="One outgoing pass then exit (debug)")
    parser.add_argument("--watch", action="store_true",
                        help="Continuous (default behavior)")
    args = parser.parse_args()

    asyncio.run(main_async(once=args.once))


if __name__ == "__main__":
    main()
