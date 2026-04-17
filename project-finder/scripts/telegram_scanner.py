#!/usr/bin/env python3
"""
ProjectFinder Telegram Scanner — local channel reader.

Reads enabled Telegram channels from sources.json via the Telethon library
and writes new job postings into the `jobs` table in data/projectfinder.sqlite
via pf_db.upsert_job (deduplicated by message URL through the `seen` table).

Cowork sandbox cannot reach Telegram MTProto servers — this script runs locally.

Usage:
    python3 telegram_scanner.py             # one-shot scan and exit
    python3 telegram_scanner.py --watch     # continuous: scan every N minutes
    python3 telegram_scanner.py --interval 30   # custom watch interval (minutes)

First run:
    pip install telethon
    python3 telegram_scanner.py
    # Telethon will ask for your phone number, then SMS/Telegram code.
    # After that, session is saved — no more prompts.

Requirements: Python 3.7+, telethon (pip install telethon)
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, Exception):
    pass

try:
    from telethon.sync import TelegramClient
    from telethon.errors import (
        ChannelPrivateError,
        UsernameNotOccupiedError,
        FloodWaitError,
    )
except ImportError:
    print("ERROR: telethon not installed. Run: pip install telethon")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SOURCES_FILE = PROJECT_ROOT / "config" / "sources.json"
POSITIONS_FILE = PROJECT_ROOT / "config" / "positions.json"

sys.path.insert(0, str(SCRIPT_DIR))
import pf_db       # noqa: E402
import pf_secrets  # noqa: E402

# Common job indicators across languages
JOB_INDICATORS = {
    "hiring", "vacancy", "looking for", "we are hiring", "ищем", "вакансия",
    "требуется", "нужен", "нужна", "позиция", "developer", "разработчик",
    "engineer", "remote", "удалённо", "удаленно", "удалёнка", "удаленка", "fulltime",
    "fullstack", "frontend", "backend",
}

# Email and Telegram username regexes
EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
TG_USERNAME_RE = re.compile(r"@([a-zA-Z][a-zA-Z0-9_]{3,31})")
URL_RE = re.compile(r"https?://[^\s)\]]+")


def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def build_keyword_set(positions: dict) -> set:
    """Combine all keywords from positions.json + common job indicators."""
    keywords = set(JOB_INDICATORS)
    for pos in positions.get("positions", []):
        kw = pos.get("keywords", {})
        for key in ("must_match", "nice_to_have", "role_indicators"):
            for w in kw.get(key, []):
                keywords.add(w.lower())
    return keywords


def message_looks_like_job(text: str, keywords: set) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    for kw in keywords:
        if kw in text_lower:
            return True
    return False


def extract_contact(text: str, channel_handle: str) -> dict:
    """Pull email or @username from message text."""
    email_match = EMAIL_RE.search(text or "")
    # Find usernames, but exclude the channel itself
    usernames = TG_USERNAME_RE.findall(text or "")
    usernames = [
        u for u in usernames if u.lower() != channel_handle.lstrip("@").lower()
    ]

    contact = {"telegram": None, "email": None, "type": "none"}
    if email_match:
        contact["email"] = email_match.group(0)
        contact["type"] = "email"
    if usernames:
        # Prefer @bot pattern detection — bots usually end in 'bot' or 'Bot'
        first = usernames[0]
        contact["telegram"] = "@" + first
        if first.lower().endswith("bot"):
            contact["type"] = "bot"
        elif contact["type"] != "email":
            contact["type"] = "username"
    if (
        contact["type"] == "none"
        and text
        and ("ЛС" in text or "DM" in text or "личку" in text.lower())
    ):
        contact["type"] = "dm"
    return contact


def derive_title(text: str, channel_name: str) -> str:
    """Take first non-empty line as title (capped at 100 chars)."""
    if not text:
        return f"(no text) — {channel_name}"
    for line in text.split("\n"):
        line = line.strip()
        if line:
            return line[:100]
    return f"(empty) — {channel_name}"


DEBUG = False


def scan_channel(client, source: dict, keywords: set,
                 max_messages: int, max_age_days: int) -> list:
    """Scan one Telegram channel, return list of new job entries."""
    handle = source.get("telegram_handle") or source.get("url", "").rsplit("/", 1)[-1]
    handle = handle.lstrip("@")
    source_id = source["id"]
    channel_name = source.get("name", handle)

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    new_jobs = []

    try:
        log(f"  scanning {channel_name} (@{handle})...")
        # iter_messages returns newest first; pinned messages may appear out of order,
        # so we use `continue` (skip) instead of `break` when a message is too old.
        messages = client.iter_messages(handle, limit=max_messages)
        scanned = 0
        too_old_count = 0
        for msg in messages:
            scanned += 1
            if msg.date and msg.date < cutoff:
                too_old_count += 1
                # Stop only if many consecutive old messages (likely past the cutoff naturally)
                if too_old_count > 10:
                    break
                continue

            text = msg.text or msg.message or msg.raw_text or ""

            if DEBUG:
                preview = (text[:120] + "...") if len(text) > 120 else text
                preview = preview.replace("\n", " | ")
                matched = message_looks_like_job(text, keywords)
                log(f"    [debug] msg #{msg.id} ({msg.date}) — len={len(text)} "
                    f"matched={matched} | {preview!r}")

            if not message_looks_like_job(text, keywords):
                continue

            url = f"https://t.me/{handle}/{msg.id}"
            # Dedup against seen_message_ids. mark_seen returns False for a
            # repeat (we leave the insert for after enrichment so we only
            # "claim" URLs we actually produced a job entry for — safe because
            # jobs.url is also UNIQUE at the DB level).
            if pf_db.has_seen("telegram", url):
                if DEBUG:
                    log(f"    [debug] msg #{msg.id} skipped (duplicate)")
                continue

            contact = extract_contact(text, handle)
            external_links = URL_RE.findall(text)
            external_job_url = None
            for link in external_links:
                if "t.me/" not in link:
                    external_job_url = link
                    break

            new_jobs.append({
                "id": f"{source_id}-{msg.date.strftime('%Y-%m-%d')}-{msg.id}",
                "source_id": source_id,
                "url": url,
                "external_job_url": external_job_url,
                "title": derive_title(text, channel_name),
                "company": None,  # hard to reliably extract
                "discovered_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "raw_description": text,
                "telegram_message_text": text,
                "contact": contact,
                "message_id": msg.id,
                "message_date": msg.date.strftime("%Y-%m-%d %H:%M:%S"),
                "matched_position": None,
                "score": "pending_evaluation",
                "score_value": None,
                "score_details": "",
                "status": "pending_evaluation",
                "draft_file": None,
                "employer_email": contact.get("email"),
                "gmail_draft_created": False,
                "send_method": None,
                "reviewed": False,
                "review_decision": None,
                "language": "ru" if source.get("language") == "ru" else "mixed",
                "notes": "Found by telegram_scanner.py (local). Awaiting evaluation by Cowork.",
            })
        log(f"    scanned {scanned} messages, found {len(new_jobs)} new jobs")
    except UsernameNotOccupiedError:
        log(f"    ERROR: @{handle} does not exist")
    except ChannelPrivateError:
        log(f"    ERROR: @{handle} is private — need to join first")
    except FloodWaitError as e:
        log(f"    FLOOD WAIT: must wait {e.seconds}s before retrying")
        time.sleep(min(e.seconds, 60))
    except Exception as e:
        log(f"    ERROR scanning @{handle}: {e}")

    return new_jobs


def run_scan() -> int:
    """One full scan cycle. Returns number of new jobs added."""
    config = pf_secrets.load_config("telegram-client-config.json")
    sources_data = load_json(SOURCES_FILE)
    positions = load_json(POSITIONS_FILE)

    # Ensure DB exists.
    pf_db.init_db()

    tg = config.get("telegram_client", {})
    if not tg.get("enabled"):
        log("Telegram client disabled in config.")
        return 0

    api_id = tg["api_id"]
    api_hash = tg["api_hash"]
    session_name = tg.get("session_name", "projectfinder")
    session_path = SCRIPT_DIR / session_name

    keywords = build_keyword_set(positions)

    # Filter telegram sources
    tg_sources = [
        s
        for s in sources_data.get("sources", [])
        if s.get("scan_method") == "telegram" and s.get("enabled")
    ]
    if not tg_sources:
        log("No enabled Telegram sources in sources.json.")
        return 0

    log(f"Scanning {len(tg_sources)} Telegram channels...")
    scan_settings = config.get("scan_settings", {})
    max_msgs = scan_settings.get("max_messages_per_channel", 100)
    max_age = scan_settings.get("max_age_days", 7)

    all_new: list[dict] = []
    with TelegramClient(str(session_path), api_id, api_hash) as client:
        for src in tg_sources:
            new_jobs = scan_channel(client, src, keywords, max_msgs, max_age)
            all_new.extend(new_jobs)

    added = 0
    for j in all_new:
        # INSERT jobs + mark_seen in a single transaction so scan resumes are
        # safe even if we crash between.
        with pf_db.transaction():
            job_record = {
                "id": j["id"],
                "url": j["url"],
                "source_id": j["source_id"],
                "channel": "telegram",
                "title": j["title"],
                "company": j.get("company"),
                "description": j.get("raw_description"),
                "contact": j.get("contact", {}).get("email")
                           or j.get("contact", {}).get("telegram"),
                "discovered_at": pf_db.utcnow_iso(),
                "raw": j,
                "status": "new",
            }
            pf_db.upsert_job(job_record)
            pf_db.mark_seen("telegram", j["url"])
            added += 1

    # Update service_state.telegram_scanner for last-scan metadata.
    def upd(st):
        st = st or {}
        st["last_telegram_scan"] = pf_db.utcnow_iso()
        st["telegram_jobs_total"] = st.get("telegram_jobs_total", 0) + added
        return st
    pf_db.state_update("telegram_scanner", upd, default={})

    if added:
        log(f"Added {added} new jobs to DB")
    else:
        log("No new jobs to add.")

    return added


def main() -> None:
    parser = argparse.ArgumentParser(description="ProjectFinder Telegram scanner")
    parser.add_argument("--watch", action="store_true", help="Watch mode")
    parser.add_argument(
        "--interval", type=int, default=30,
        help="Watch interval in minutes (default: 30)",
    )
    parser.add_argument("--debug", action="store_true",
                        help="Show every scanned message and filter decision")
    args = parser.parse_args()

    global DEBUG
    DEBUG = args.debug

    if args.watch:
        log(f"Watch mode (interval: {args.interval} min). Ctrl+C to stop.")
        try:
            while True:
                count = run_scan()
                log(f"Cycle done. {count} new. Sleeping {args.interval} min...")
                time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            log("Stopped by user.")
    else:
        count = run_scan()
        log(f"Done. {count} new jobs.")


if __name__ == "__main__":
    main()
