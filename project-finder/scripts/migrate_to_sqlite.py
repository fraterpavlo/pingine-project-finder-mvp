#!/usr/bin/env python3
"""
One-shot migration: import every existing data/*.json into projectfinder.sqlite.

Safe to re-run: all inserts use INSERT OR IGNORE or ON CONFLICT, so re-running
never duplicates rows. Original JSON files are NOT deleted; they're renamed to
*.json.legacy after a successful pass so nothing is lost and the migration is
easy to reverse.

Usage:
    python3 scripts/migrate_to_sqlite.py            # migrate + archive JSONs
    python3 scripts/migrate_to_sqlite.py --dry-run  # report only, don't write
    python3 scripts/migrate_to_sqlite.py --keep     # migrate, keep JSONs as-is
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import pf_db  # noqa: E402


DATA_DIR = SCRIPT_DIR.parent / "data"


def _load(name: str) -> dict:
    p = DATA_DIR / name
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def _archive(name: str) -> None:
    src = DATA_DIR / name
    if not src.exists():
        return
    dst = src.with_suffix(src.suffix + ".legacy")
    # If dst exists, add a timestamp suffix to never overwrite a previous archive.
    if dst.exists():
        dst = src.with_suffix(src.suffix + f".legacy.{int(time.time())}")
    src.rename(dst)
    print(f"  archived: {src.name} -> {dst.name}")


# ---------------------------------------------------------------------------

def migrate_jobs(dry: bool) -> int:
    data = _load("found-jobs.json")
    jobs = data.get("jobs", [])
    inserted = 0
    for j in jobs:
        url = j.get("url")
        if not url:
            continue
        # Map fields from found-jobs shape to jobs table.
        record = {
            "id": j.get("id"),
            "url": url,
            "source_id": j.get("source_id"),
            "channel": "telegram" if (j.get("source_id") or "").startswith("tg-") else (
                "email" if j.get("employer_email") else "web"
            ),
            "title": j.get("title"),
            "company": j.get("company"),
            "location": j.get("location"),
            "description": j.get("raw_description") or j.get("description"),
            "contact": j.get("employer_email") or j.get("employer_contact") or j.get("telegram_contact"),
            "discovered_at": j.get("discovered_at") or j.get("discovered_date") or pf_db.utcnow_iso(),
            "raw": j,
            "status": _map_job_status(j),
            "match": {
                "matched_position": j.get("matched_position"),
                "score": j.get("score"),
                "score_value": j.get("score_value"),
                "score_details": j.get("score_details"),
                "language": j.get("language"),
            } if j.get("matched_position") else None,
        }
        if not dry:
            pf_db.upsert_job(record)
        inserted += 1
    print(f"jobs:          {inserted} imported (of {len(jobs)} in JSON)")
    return inserted


def _map_job_status(j: dict) -> str:
    s = j.get("status", "new")
    # Old labels -> new controlled vocabulary.
    mapping = {
        "pending_review": "matched",
        "approved": "matched",
        "contacted": "matched",
        "rejected": "rejected",
        "archived": "archived",
        "new": "new",
    }
    return mapping.get(s, "new")


def migrate_conversations(dry: bool) -> tuple[int, int]:
    data = _load("conversations.json")
    convs = data.get("conversations", [])
    conv_n = 0
    msg_n = 0
    for c in convs:
        cid = c.get("id")
        if not cid:
            continue
        record = {
            "id": cid,
            "job_id": c.get("job_id"),
            "developer_id": c.get("developer_id"),
            "channel": c.get("channel") or "email",
            "employer_contact": c.get("employer_contact"),
            "status": c.get("status", "active"),
            "created_at": c.get("created_at") or pf_db.utcnow_iso(),
            "last_activity": c.get("last_activity") or c.get("created_at") or pf_db.utcnow_iso(),
            "test_scenario": "true" if c.get("test_scenario") else None,
            "meta": {k: v for k, v in c.items() if k not in {
                "id", "job_id", "developer_id", "channel", "employer_contact", "status",
                "created_at", "last_activity", "test_scenario", "messages"}
            },
        }
        if not dry:
            pf_db.create_conversation(record)
        conv_n += 1
        # Idempotency: if this conversation already has messages, skip appending
        # (re-running migration must not duplicate rows).
        if not dry:
            existing = pf_db.get_db().execute(
                "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = ?",
                (cid,),
            ).fetchone()[0]
            if existing:
                continue
        for m in c.get("messages", []):
            msg_record = {
                "timestamp": m.get("timestamp"),
                "direction": m.get("direction", "outgoing"),
                "content": m.get("content"),
                "channel_message_id": m.get("channel_message_id"),
                "confidence": m.get("confidence"),
                "status": m.get("status"),
                "draft_file": m.get("draft_file"),
                "outgoing_id": m.get("outgoing_id"),
                "incoming_id": m.get("incoming_id"),
                "facts_used": m.get("facts_used"),
                "review_notes": m.get("review_notes"),
            }
            if not dry:
                pf_db.append_conversation_message(cid, msg_record)
            msg_n += 1
    print(f"conversations: {conv_n} imported, {msg_n} conversation_messages")
    return conv_n, msg_n


def migrate_outgoing(dry: bool) -> int:
    data = _load("outgoing.json")
    msgs = data.get("messages", [])
    n = 0
    for m in msgs:
        oid = m.get("id")
        if not oid or not m.get("recipient") or not m.get("body"):
            continue
        if not dry:
            # Raw INSERT to preserve sent_at/rejected_at/etc. without going
            # through the state-machine helpers.
            conn = pf_db.get_db()
            conn.execute(
                """
                INSERT OR IGNORE INTO outgoing_messages (
                    id, conversation_id, job_id, developer_id, channel, recipient,
                    subject, body, status, is_reply, is_first_message, confidence,
                    created_at, approved_at, rejected_at, edited_at, edited_by,
                    sent_at, send_error, channel_message_id, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    oid,
                    m.get("conversation_id"),
                    m.get("job_id"),
                    m.get("developer_id"),
                    m.get("channel") or "email",
                    m.get("recipient"),
                    m.get("subject"),
                    m.get("body"),
                    m.get("status") or "needs_review",
                    1 if m.get("is_reply") else 0,
                    1 if m.get("is_first_message") else 0,
                    m.get("confidence"),
                    m.get("created_at") or pf_db.utcnow_iso(),
                    m.get("approved_at"),
                    m.get("rejected_at"),
                    m.get("edited_at"),
                    m.get("edited_by"),
                    m.get("sent_at"),
                    m.get("send_error"),
                    m.get("channel_message_id"),
                    m.get("notes"),
                ),
            )
        n += 1
    print(f"outgoing:      {n} imported (of {len(msgs)} in JSON)")
    return n


def migrate_incoming(dry: bool) -> int:
    data = _load("incoming.json")
    msgs = data.get("messages", [])
    n = 0
    for m in msgs:
        mid = m.get("id")
        if not mid:
            continue
        record = {
            "id": mid,
            "conversation_id": m.get("conversation_id"),
            "job_id": m.get("job_id"),
            "channel": m.get("channel") or "email",
            "sender": m.get("sender"),
            "subject": m.get("subject"),
            "text": m.get("text"),
            "received_at": m.get("received_at") or pf_db.utcnow_iso(),
            "imap_message_id": m.get("imap_message_id"),
            "tg_message_id": m.get("tg_message_id"),
            "status": m.get("status", "new"),
            "raw": {k: v for k, v in m.items() if k not in {
                "id", "conversation_id", "job_id", "channel", "sender", "subject",
                "text", "received_at", "imap_message_id", "tg_message_id", "status"}
            },
        }
        if not dry:
            # Preserve processed_at directly.
            conn = pf_db.get_db()
            conn.execute(
                """
                INSERT OR IGNORE INTO incoming_messages (
                    id, conversation_id, job_id, channel, sender, subject, text,
                    received_at, imap_message_id, tg_message_id, status,
                    processed_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"], record["conversation_id"], record["job_id"],
                    record["channel"], record["sender"], record["subject"],
                    record["text"], record["received_at"], record["imap_message_id"],
                    record["tg_message_id"], record["status"],
                    m.get("processed_at"),
                    json.dumps(record["raw"], ensure_ascii=False) if record["raw"] else None,
                ),
            )
        n += 1
    print(f"incoming:      {n} imported (of {len(msgs)} in JSON)")
    return n


def migrate_notifications(dry: bool) -> int:
    data = _load("notifications.json")
    items = data.get("notifications", [])
    n = 0
    for x in items:
        nid = x.get("id")
        if not nid:
            continue
        if not dry:
            conn = pf_db.get_db()
            conn.execute(
                """
                INSERT OR IGNORE INTO notifications (
                    id, created_at, type, urgency, job_id, job_title, conversation_id,
                    outgoing_id, draft_file, escalation_file, reason, summary,
                    recipient, telegram_chat_id, message_sent, telegram_status,
                    telegram_response, acknowledged, acknowledged_at, sent_at,
                    test_entry
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    nid,
                    x.get("created_at") or pf_db.utcnow_iso(),
                    x.get("type"),
                    x.get("urgency"),
                    x.get("job_id"),
                    x.get("job_title"),
                    x.get("conversation_id"),
                    x.get("outgoing_id"),
                    x.get("draft_file"),
                    x.get("escalation_file"),
                    x.get("reason"),
                    x.get("summary"),
                    x.get("recipient"),
                    x.get("telegram_chat_id"),
                    x.get("message_sent"),
                    x.get("telegram_status") or "pending",
                    json.dumps(x.get("telegram_response"), ensure_ascii=False) if x.get("telegram_response") else None,
                    1 if x.get("acknowledged") else 0,
                    x.get("acknowledged_at"),
                    x.get("sent_at"),
                    1 if x.get("test_entry") else 0,
                ),
            )
        n += 1
    print(f"notifications: {n} imported (of {len(items)} in JSON)")
    return n


def migrate_escalations(dry: bool) -> int:
    data = _load("escalations.json")
    items = data.get("escalations", [])
    n = 0
    for e in items:
        eid = e.get("id")
        if not eid:
            continue
        if not dry:
            record = {
                "id": eid,
                "conversation_id": e.get("conversation_id"),
                "job_id": e.get("job_id"),
                "developer_id": e.get("developer_id"),
                "channel": e.get("channel"),
                "created_at": e.get("created_at") or pf_db.utcnow_iso(),
                "incoming_message": e.get("incoming_message"),
                "reason": e.get("reason"),
                "suggested_human_action": e.get("suggested_human_action"),
                "status": e.get("status") or "open",
                "priority": e.get("priority"),
            }
            conn = pf_db.get_db()
            conn.execute(
                """
                INSERT OR IGNORE INTO escalations (
                    id, conversation_id, job_id, developer_id, channel, created_at,
                    incoming_message, reason, suggested_human_action, status, priority
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (record["id"], record["conversation_id"], record["job_id"],
                 record["developer_id"], record["channel"], record["created_at"],
                 record["incoming_message"], record["reason"],
                 record["suggested_human_action"], record["status"], record["priority"]),
            )
        n += 1
    print(f"escalations:   {n} imported (of {len(items)} in JSON)")
    return n


def migrate_state(dry: bool) -> int:
    """email_io_state.json, bot_handler_state.json → service_state key-value."""
    n = 0

    email_state = _load("email_io_state.json")
    if email_state:
        if not dry:
            pf_db.state_set("email_io_state", email_state)
        # Also mirror seen_message_ids into the dedicated seen_message_ids table.
        for sid in email_state.get("seen_message_ids", []):
            if not dry:
                pf_db.mark_seen("email", sid)
        n += 1

    bot_state = _load("bot_handler_state.json")
    if bot_state:
        if not dry:
            pf_db.state_set("bot_handler_state", bot_state)
        n += 1

    print(f"state:         {n} key(s) imported into service_state")
    return n


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't write, just report")
    ap.add_argument("--keep", action="store_true", help="keep JSON files (don't archive)")
    args = ap.parse_args()

    if args.dry_run:
        print(">>> DRY RUN — no writes <<<")
    pf_db.init_db()

    print()
    print("Migrating JSON -> SQLite:")
    print(f"  DB file: {pf_db.DB_PATH}")
    print()

    migrate_jobs(args.dry_run)
    migrate_conversations(args.dry_run)
    migrate_outgoing(args.dry_run)
    migrate_incoming(args.dry_run)
    migrate_notifications(args.dry_run)
    migrate_escalations(args.dry_run)
    migrate_state(args.dry_run)

    print()
    print("Final counts:")
    for k, v in pf_db.counts().items():
        print(f"  {k:28s} {v}")

    if not args.dry_run and not args.keep:
        print()
        print("Archiving JSON files (-> *.legacy):")
        for name in [
            "found-jobs.json", "conversations.json", "outgoing.json",
            "incoming.json", "notifications.json", "escalations.json",
            "email_io_state.json", "bot_handler_state.json", "inbox.json",
        ]:
            _archive(name)

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
