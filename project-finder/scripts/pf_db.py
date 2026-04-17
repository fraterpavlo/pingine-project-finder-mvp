#!/usr/bin/env python3
"""
ProjectFinder SQLite data layer.

One SQLite file (project-finder/data/projectfinder.sqlite) is the single source of
truth for all state: jobs, conversations, messages (incoming/outgoing),
notifications, escalations, service state, deduplication IDs.

Key reliability features compared to the previous JSON-file approach:

1. Multi-process safe writes via WAL mode + busy_timeout. No lock files needed.
2. No read-modify-write lost updates: every mutation is a single SQL statement.
3. Two-phase commit for sending messages:
     ready --(atomic claim)--> sending --(send ok)--> sent
                                        --(send err)--> failed
   Only ONE worker can claim a message (UPDATE ... WHERE status='ready' returns
   rowcount). On crash mid-send, recover_stuck_sending() requeues them.
4. Dedup via UNIQUE constraints on jobs.url, (channel,imap_message_id), and the
   seen_message_ids table — impossible to double-insert by construction.
5. Idempotent status transitions: mark_*() functions use WHERE guards so a
   duplicate call cannot move the state backwards or re-trigger a side effect.

Usage:
    import pf_db
    pf_db.init_db()              # safe to call every start; creates tables
    conn = pf_db.get_db()        # per-process singleton connection
    pf_db.insert_job({...})
    for row in pf_db.list_pending_outgoing(): ...

Connection model: one sqlite3.Connection per process, cached module-level. All
public functions use this shared connection. Scripts run as separate OS
processes (launched by projectfinder.py), so there's no cross-thread sharing
inside a single interpreter.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, Exception):
    pass


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent                 # project-finder/
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
DB_PATH = DATA_DIR / "projectfinder.sqlite"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utcnow_iso() -> str:
    """ISO-8601 UTC timestamp with 'Z' suffix — human-readable, sortable."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str = "") -> str:
    """Short unique id: <prefix>-<hex8>."""
    h = uuid.uuid4().hex[:8]
    return f"{prefix}-{h}" if prefix else h


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_conn: Optional[sqlite3.Connection] = None


def _try_pragma(conn: sqlite3.Connection, sql: str) -> bool:
    """Run a PRAGMA; return True on success, False on OperationalError.
    We log to stderr only; pf_db.py must not crash for a cosmetic pragma."""
    try:
        conn.execute(sql)
        return True
    except sqlite3.OperationalError as e:
        print(f"[pf_db] PRAGMA failed: {sql!r} -> {e}", file=sys.stderr, flush=True)
        return False


# Module-level record of which journal mode we ended up with. Populated inside
# get_db() on the first connection; downstream code (health_check, reset_db,
# migrations) can inspect it without re-querying.
CURRENT_JOURNAL_MODE: Optional[str] = None


def get_db() -> sqlite3.Connection:
    """Return a process-wide sqlite3 connection.

    Preferred journal mode is WAL (multi-process concurrency). On filesystems
    that don't support WAL's shared-memory locking — notably FUSE-mounted user
    folders from Cowork sandboxes — WAL setup fails with OperationalError
    ("disk I/O error" / "unable to open database file"). In that case we fall
    back to TRUNCATE, then the SQLite default DELETE mode. Both are slower and
    single-writer, but they actually work over FUSE.

    The journal mode is stored in the DB file header, so if a local daemon
    later opens the DB and sets WAL, that persists; the Cowork agent's next
    run will get WAL again and only fall back if it can't.
    """
    global _conn, CURRENT_JOURNAL_MODE
    if _conn is not None:
        return _conn

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(DB_PATH),
        check_same_thread=False,
        isolation_level=None,           # autocommit; we use BEGIN IMMEDIATE for txns
        timeout=15.0,                   # wait up to 15s if another writer has the lock
    )
    conn.row_factory = sqlite3.Row

    # Try WAL → TRUNCATE → leave default (DELETE). Record whichever stuck.
    mode = None
    for candidate in ("WAL", "TRUNCATE", "DELETE"):
        try:
            row = conn.execute(f"PRAGMA journal_mode={candidate}").fetchone()
            if row and row[0] and row[0].lower() == candidate.lower():
                mode = candidate
                break
        except sqlite3.OperationalError as e:
            print(
                f"[pf_db] journal_mode={candidate} not available: {e}",
                file=sys.stderr,
                flush=True,
            )
            continue
    CURRENT_JOURNAL_MODE = mode or "unknown"
    if CURRENT_JOURNAL_MODE != "WAL":
        print(
            f"[pf_db] journal_mode fallback: using {CURRENT_JOURNAL_MODE} "
            f"(WAL not available — likely FUSE/sandbox filesystem)",
            file=sys.stderr,
            flush=True,
        )

    # These are safe to attempt even if they fail individually.
    _try_pragma(conn, "PRAGMA synchronous=NORMAL")
    _try_pragma(conn, "PRAGMA foreign_keys=ON")
    _try_pragma(conn, "PRAGMA busy_timeout=15000")

    _conn = conn
    return conn


def health_check() -> dict:
    """Run PRAGMA integrity_check and report journal mode + row counts.

    Call this at the start of every scheduled task and every daemon main(). If
    the result contains anything other than 'ok', log it loudly and skip
    destructive operations — working on a corrupt DB only makes it worse.

    Return shape:
        {"ok": bool, "journal_mode": str, "integrity": "ok" | [errors],
         "row_counts": {"jobs": n, "conversations": n, ...}}
    """
    conn = get_db()
    try:
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        integrity = [r[0] for r in integrity_rows]
        if integrity == ["ok"]:
            integrity_result: Any = "ok"
            ok = True
        else:
            integrity_result = integrity
            ok = False
    except sqlite3.DatabaseError as e:
        integrity_result = f"integrity_check raised: {e}"
        ok = False

    counts: dict[str, int] = {}
    for tbl in ("jobs", "conversations", "conversation_messages",
                "incoming_messages", "outgoing_messages",
                "notifications", "escalations", "seen_message_ids"):
        try:
            counts[tbl] = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        except sqlite3.DatabaseError as e:
            counts[tbl] = -1
            ok = False
            print(f"[pf_db.health_check] SELECT count FROM {tbl} -> {e}",
                  file=sys.stderr, flush=True)

    result = {
        "ok": ok,
        "journal_mode": CURRENT_JOURNAL_MODE,
        "integrity": integrity_result,
        "row_counts": counts,
    }
    if not ok:
        print(f"[pf_db.health_check] UNHEALTHY: {result}",
              file=sys.stderr, flush=True)
    return result


def close_db() -> None:
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        finally:
            _conn = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL UNIQUE,
    source_id       TEXT,
    channel         TEXT,                           -- 'telegram'|'email'|'web'|'other'
    title           TEXT,
    company         TEXT,
    location        TEXT,
    description     TEXT,
    contact         TEXT,                           -- raw contact (handle/email/url)
    discovered_at   TEXT NOT NULL,
    raw_json        TEXT,                           -- full raw payload from scanner
    status          TEXT NOT NULL DEFAULT 'new',    -- new|evaluated|matched|rejected|archived
    match_json      TEXT,                           -- evaluation result (positions, score, etc.)
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_status      ON jobs(status);
CREATE INDEX IF NOT EXISTS ix_jobs_source      ON jobs(source_id);
CREATE INDEX IF NOT EXISTS ix_jobs_discovered  ON jobs(discovered_at);


CREATE TABLE IF NOT EXISTS conversations (
    id                 TEXT PRIMARY KEY,
    job_id             TEXT,
    developer_id       TEXT,
    channel            TEXT NOT NULL,               -- 'telegram'|'email'
    employer_contact   TEXT,
    status             TEXT NOT NULL DEFAULT 'active',  -- active|closed|escalated|rejected
    created_at         TEXT NOT NULL,
    last_activity      TEXT NOT NULL,
    test_scenario      TEXT,
    meta_json          TEXT
);
CREATE INDEX IF NOT EXISTS ix_conv_job      ON conversations(job_id);
CREATE INDEX IF NOT EXISTS ix_conv_status   ON conversations(status);
CREATE INDEX IF NOT EXISTS ix_conv_channel  ON conversations(channel);


-- Normalised messages inside a conversation. Previously these lived as a JSON
-- array inside conversations.json, which is what caused concurrent-write loss.
-- Each row is now an independent INSERT/UPDATE.
CREATE TABLE IF NOT EXISTS conversation_messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     TEXT NOT NULL,
    ts                  TEXT NOT NULL,
    direction           TEXT NOT NULL,               -- 'incoming'|'outgoing'
    content             TEXT,
    channel_message_id  TEXT,
    confidence          TEXT,
    status              TEXT,
    draft_file          TEXT,
    outgoing_id         TEXT,                        -- FK -> outgoing_messages.id
    incoming_id         TEXT,                        -- FK -> incoming_messages.id
    facts_used_json     TEXT,
    review_notes        TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);
CREATE INDEX IF NOT EXISTS ix_convmsg_conv ON conversation_messages(conversation_id, ts);


CREATE TABLE IF NOT EXISTS incoming_messages (
    id                TEXT PRIMARY KEY,
    conversation_id   TEXT,
    job_id            TEXT,
    channel           TEXT NOT NULL,                 -- 'email'|'telegram'
    sender            TEXT,
    subject           TEXT,
    text              TEXT,
    received_at       TEXT NOT NULL,
    imap_message_id   TEXT,
    tg_message_id     TEXT,
    status            TEXT NOT NULL DEFAULT 'new',   -- new|processed
    processed_at      TEXT,
    raw_json          TEXT
);
CREATE INDEX IF NOT EXISTS ix_inc_status     ON incoming_messages(status);
CREATE INDEX IF NOT EXISTS ix_inc_conv       ON incoming_messages(conversation_id);
CREATE INDEX IF NOT EXISTS ix_inc_channel    ON incoming_messages(channel);
-- Partial unique indexes for dedup by channel-specific external id
CREATE UNIQUE INDEX IF NOT EXISTS uq_inc_email_imap
    ON incoming_messages(imap_message_id)
    WHERE channel = 'email' AND imap_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_inc_tg
    ON incoming_messages(tg_message_id, sender)
    WHERE channel = 'telegram' AND tg_message_id IS NOT NULL;


CREATE TABLE IF NOT EXISTS outgoing_messages (
    id                  TEXT PRIMARY KEY,
    conversation_id     TEXT,
    job_id              TEXT,
    developer_id        TEXT,
    channel             TEXT NOT NULL,               -- 'email'|'telegram'
    recipient           TEXT NOT NULL,
    subject             TEXT,
    body                TEXT NOT NULL,
    status              TEXT NOT NULL,               -- needs_review|ready|sending|sent|failed|rejected
    is_reply            INTEGER NOT NULL DEFAULT 0,  -- 0/1
    is_first_message    INTEGER NOT NULL DEFAULT 0,  -- 0/1
    confidence          TEXT,                        -- HIGH|MEDIUM|LOW
    created_at          TEXT NOT NULL,
    approved_at         TEXT,
    rejected_at         TEXT,
    edited_at           TEXT,
    edited_by           TEXT,
    claimed_at          TEXT,                        -- when moved to 'sending'
    attempt_id          TEXT,                        -- claim UUID; NULL when not sending
    send_attempts       INTEGER NOT NULL DEFAULT 0,
    sent_at             TEXT,
    send_error          TEXT,
    channel_message_id  TEXT,                        -- assigned after send
    notes               TEXT
);
CREATE INDEX IF NOT EXISTS ix_out_status  ON outgoing_messages(status);
CREATE INDEX IF NOT EXISTS ix_out_conv    ON outgoing_messages(conversation_id);
CREATE INDEX IF NOT EXISTS ix_out_channel ON outgoing_messages(channel, status);


CREATE TABLE IF NOT EXISTS notifications (
    id                 TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,
    type               TEXT,
    urgency            TEXT,
    job_id             TEXT,
    job_title          TEXT,
    conversation_id    TEXT,
    outgoing_id        TEXT,
    draft_file         TEXT,
    escalation_file    TEXT,
    reason             TEXT,
    summary            TEXT,
    recipient          TEXT,
    telegram_chat_id   TEXT,
    message_sent       TEXT,
    telegram_status    TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|failed
    telegram_response  TEXT,
    acknowledged       INTEGER NOT NULL DEFAULT 0,
    acknowledged_at    TEXT,
    sent_at            TEXT,
    test_entry         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_notif_tg_status   ON notifications(telegram_status);
CREATE INDEX IF NOT EXISTS ix_notif_ack         ON notifications(acknowledged);


CREATE TABLE IF NOT EXISTS escalations (
    id                        TEXT PRIMARY KEY,
    conversation_id           TEXT,
    job_id                    TEXT,
    developer_id              TEXT,
    channel                   TEXT,
    created_at                TEXT NOT NULL,
    incoming_message          TEXT,
    reason                    TEXT,
    suggested_human_action    TEXT,
    status                    TEXT NOT NULL DEFAULT 'open',   -- open|resolved|dismissed
    priority                  TEXT
);
CREATE INDEX IF NOT EXISTS ix_esc_status ON escalations(status);


CREATE TABLE IF NOT EXISTS service_state (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);


-- Cross-source deduplication of external message/post IDs (e.g. Telegram scan
-- already saw 't.me/jobsdevby/12345'). Single UNIQUE key => INSERT OR IGNORE
-- semantics for idempotent scanning.
CREATE TABLE IF NOT EXISTS seen_message_ids (
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    seen_at      TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);
"""


def _migrate_add_column(conn: sqlite3.Connection, table: str,
                        column: str, coltype: str) -> None:
    """Безопасно добавить колонку, если её нет. Нужна для эволюции схемы
    между прогонами: `CREATE TABLE IF NOT EXISTS` поверх существующей таблицы
    не добавляет новые колонки, и мы не хотим пересоздавать таблицу.
    """
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db() -> None:
    """Create all tables/indexes if they don't exist. Idempotent.
    Also applies in-place migrations for columns that were added after
    the initial schema (retry_count, next_retry_at)."""
    conn = get_db()
    conn.executescript(SCHEMA_SQL)
    # Миграция 1 (2026-04-16): retry-поля в outgoing_messages/notifications.
    # До этого failed-сообщения никем не перезабирались — любой сетевой сбой
    # оставлял запись в failed навсегда.
    _migrate_add_column(conn, "outgoing_messages", "retry_count", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(conn, "outgoing_messages", "next_retry_at", "TEXT")
    _migrate_add_column(conn, "notifications", "retry_count", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(conn, "notifications", "next_retry_at", "TEXT")


# ---------------------------------------------------------------------------
# Transaction helper
# ---------------------------------------------------------------------------

class _Tx:
    """with pf_db.transaction(): ... wraps BEGIN IMMEDIATE / COMMIT / ROLLBACK."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            self.conn.execute("COMMIT")
        else:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
        return False


def transaction() -> _Tx:
    return _Tx(get_db())


def _j(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _unj(value: Optional[str]) -> Any:
    if value is None or value == "":
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def upsert_job(job: dict) -> str:
    """Insert a job if new, otherwise update mutable fields. Returns job id.

    Dedup is by the UNIQUE url column — the scanner can replay safely.
    Expected keys: url (required), id (optional), source_id, channel, title,
    company, location, description, contact, discovered_at, raw, status,
    match.
    """
    if not job.get("url"):
        raise ValueError("upsert_job: 'url' is required")

    job_id = job.get("id") or new_id("job")
    now = utcnow_iso()
    discovered_at = job.get("discovered_at") or now

    conn = get_db()
    # Try INSERT; if URL already exists, UPDATE the existing row.
    conn.execute(
        """
        INSERT INTO jobs (
            id, url, source_id, channel, title, company, location, description,
            contact, discovered_at, raw_json, status, match_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            source_id    = COALESCE(excluded.source_id, jobs.source_id),
            channel      = COALESCE(excluded.channel, jobs.channel),
            title        = COALESCE(excluded.title, jobs.title),
            company      = COALESCE(excluded.company, jobs.company),
            location     = COALESCE(excluded.location, jobs.location),
            description  = COALESCE(excluded.description, jobs.description),
            contact      = COALESCE(excluded.contact, jobs.contact),
            raw_json     = COALESCE(excluded.raw_json, jobs.raw_json),
            match_json   = COALESCE(excluded.match_json, jobs.match_json),
            updated_at   = excluded.updated_at
        """,
        (
            job_id,
            job["url"],
            job.get("source_id"),
            job.get("channel"),
            job.get("title"),
            job.get("company"),
            job.get("location"),
            job.get("description"),
            job.get("contact"),
            discovered_at,
            _j(job.get("raw")),
            job.get("status", "new"),
            _j(job.get("match")),
            now,
        ),
    )
    # Fetch the canonical id (either the new one, or the existing one).
    row = conn.execute("SELECT id FROM jobs WHERE url = ?", (job["url"],)).fetchone()
    return row["id"] if row else job_id


def get_job(job_id: str) -> Optional[dict]:
    row = get_db().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_dict(row)


def get_job_by_url(url: str) -> Optional[dict]:
    row = get_db().execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    return _row_to_dict(row)


def list_jobs(status: Optional[str] = None, limit: int = 500) -> list[dict]:
    conn = get_db()
    if status is None:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY discovered_at DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY discovered_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def set_job_status(job_id: str, status: str, match: Optional[dict] = None) -> None:
    get_db().execute(
        "UPDATE jobs SET status = ?, match_json = COALESCE(?, match_json), updated_at = ? WHERE id = ?",
        (status, _j(match), utcnow_iso(), job_id),
    )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def create_conversation(conv: dict) -> str:
    """conv may supply id; otherwise generated."""
    cid = conv.get("id") or new_id("conv")
    now = utcnow_iso()
    get_db().execute(
        """
        INSERT INTO conversations (
            id, job_id, developer_id, channel, employer_contact, status,
            created_at, last_activity, test_scenario, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            cid,
            conv.get("job_id"),
            conv.get("developer_id"),
            conv["channel"],
            conv.get("employer_contact"),
            conv.get("status", "active"),
            conv.get("created_at", now),
            conv.get("last_activity", now),
            conv.get("test_scenario"),
            _j(conv.get("meta")),
        ),
    )
    return cid


def get_conversation(conv_id: str) -> Optional[dict]:
    row = get_db().execute(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    return _row_to_dict(row)


def list_conversations(status: Optional[str] = None) -> list[dict]:
    conn = get_db()
    if status is None:
        rows = conn.execute(
            "SELECT * FROM conversations ORDER BY last_activity DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE status = ? ORDER BY last_activity DESC",
            (status,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def set_conversation_status(conv_id: str, status: str) -> None:
    get_db().execute(
        "UPDATE conversations SET status = ?, last_activity = ? WHERE id = ?",
        (status, utcnow_iso(), conv_id),
    )


def touch_conversation(conv_id: str) -> None:
    get_db().execute(
        "UPDATE conversations SET last_activity = ? WHERE id = ?",
        (utcnow_iso(), conv_id),
    )


def update_conversation_meta(conv_id: str, meta: dict) -> bool:
    """Replace meta_json for a conversation + touch last_activity. Idempotent.

    Используется dialogue-agent'ом для сохранения сжатой `history_summary`
    после того, как окно из 20 последних сообщений было пересчитано.
    Возвращает True если conversation существовала и строка обновлена.
    """
    cur = get_db().execute(
        "UPDATE conversations SET meta_json = ?, last_activity = ? WHERE id = ?",
        (_j(meta), utcnow_iso(), conv_id),
    )
    return cur.rowcount > 0


def find_conversation(job_id: str, channel: str,
                      employer_contact: str) -> Optional[dict]:
    """Найти активную (не closed) conversation по job + channel + contact.

    Нужна и для evaluate-and-initiate (проверить, не заведён ли разговор
    другим путём), и для dialogue-agent — чтобы отвечать в уже существующую
    нитку, а не создавать дубликат с колоночным id `conv-<job>-001`.

    Сравнение `employer_contact` — case-insensitive, т.к. email/username
    могут прийти в разном регистре.
    """
    row = get_db().execute(
        "SELECT * FROM conversations "
        "WHERE job_id = ? AND channel = ? "
        "  AND lower(COALESCE(employer_contact,'')) = lower(?) "
        "  AND status != 'closed' "
        "ORDER BY last_activity DESC LIMIT 1",
        (job_id, channel, employer_contact or ""),
    ).fetchone()
    return _row_to_dict(row)


def append_conversation_message(conv_id: str, msg: dict) -> int:
    """Add one message to a conversation. Returns new row id."""
    cur = get_db().execute(
        """
        INSERT INTO conversation_messages (
            conversation_id, ts, direction, content, channel_message_id,
            confidence, status, draft_file, outgoing_id, incoming_id,
            facts_used_json, review_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conv_id,
            msg.get("timestamp") or utcnow_iso(),
            msg["direction"],
            msg.get("content"),
            msg.get("channel_message_id"),
            msg.get("confidence"),
            msg.get("status"),
            msg.get("draft_file"),
            msg.get("outgoing_id"),
            msg.get("incoming_id"),
            _j(msg.get("facts_used")),
            msg.get("review_notes"),
        ),
    )
    touch_conversation(conv_id)
    return cur.lastrowid


def list_conversation_messages(conv_id: str) -> list[dict]:
    rows = get_db().execute(
        "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY id ASC",
        (conv_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Incoming messages
# ---------------------------------------------------------------------------

def insert_incoming(msg: dict) -> Optional[str]:
    """Insert an incoming message; returns its id, or None if it was a
    duplicate (ignored via UNIQUE constraint on imap_message_id / tg_message_id).
    """
    mid = msg.get("id") or new_id("in")
    cur = get_db().execute(
        """
        INSERT OR IGNORE INTO incoming_messages (
            id, conversation_id, job_id, channel, sender, subject, text,
            received_at, imap_message_id, tg_message_id, status, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mid,
            msg.get("conversation_id"),
            msg.get("job_id"),
            msg["channel"],
            msg.get("sender"),
            msg.get("subject"),
            msg.get("text"),
            msg.get("received_at") or utcnow_iso(),
            msg.get("imap_message_id"),
            msg.get("tg_message_id"),
            msg.get("status", "new"),
            _j(msg.get("raw")),
        ),
    )
    if cur.rowcount == 0:
        return None
    return mid


def list_new_incoming(limit: int = 200) -> list[dict]:
    rows = get_db().execute(
        "SELECT * FROM incoming_messages WHERE status = 'new' ORDER BY received_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_incoming_processed(incoming_id: str) -> bool:
    """Atomic transition new -> processed. Returns True only if actually
    transitioned (idempotent; a second call returns False)."""
    cur = get_db().execute(
        "UPDATE incoming_messages SET status='processed', processed_at=? "
        "WHERE id = ? AND status = 'new'",
        (utcnow_iso(), incoming_id),
    )
    return cur.rowcount > 0


def get_incoming(incoming_id: str) -> Optional[dict]:
    row = get_db().execute(
        "SELECT * FROM incoming_messages WHERE id = ?", (incoming_id,)
    ).fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Outgoing messages — the critical path
# ---------------------------------------------------------------------------

def insert_outgoing(msg: dict) -> str:
    """Insert a new outgoing draft/ready message. Required: channel, recipient,
    body, status. Returns id."""
    oid = msg.get("id") or new_id("out")
    get_db().execute(
        """
        INSERT INTO outgoing_messages (
            id, conversation_id, job_id, developer_id, channel, recipient,
            subject, body, status, is_reply, is_first_message, confidence,
            created_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            oid,
            msg.get("conversation_id"),
            msg.get("job_id"),
            msg.get("developer_id"),
            msg["channel"],
            msg["recipient"],
            msg.get("subject"),
            msg["body"],
            msg.get("status", "needs_review"),
            1 if msg.get("is_reply") else 0,
            1 if msg.get("is_first_message") else 0,
            msg.get("confidence"),
            msg.get("created_at") or utcnow_iso(),
            msg.get("notes"),
        ),
    )
    return oid


def get_outgoing(outgoing_id: str) -> Optional[dict]:
    row = get_db().execute(
        "SELECT * FROM outgoing_messages WHERE id = ?", (outgoing_id,)
    ).fetchone()
    return _row_to_dict(row)


def list_outgoing_by_status(status: str, channel: Optional[str] = None, limit: int = 200) -> list[dict]:
    conn = get_db()
    if channel:
        rows = conn.execute(
            "SELECT * FROM outgoing_messages WHERE status = ? AND channel = ? "
            "ORDER BY created_at ASC LIMIT ?",
            (status, channel, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM outgoing_messages WHERE status = ? ORDER BY created_at ASC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_needs_review(limit: int = 200) -> list[dict]:
    return list_outgoing_by_status("needs_review", limit=limit)


def approve_outgoing(outgoing_id: str, edited_body: Optional[str] = None,
                     edited_by: Optional[str] = None) -> bool:
    """needs_review -> ready. Idempotent: a second call returns False.
    Optionally replaces the body (when the reviewer edited it)."""
    now = utcnow_iso()
    conn = get_db()
    if edited_body is not None:
        cur = conn.execute(
            "UPDATE outgoing_messages "
            "SET status='ready', approved_at=?, body=?, edited_at=?, edited_by=? "
            "WHERE id = ? AND status = 'needs_review'",
            (now, edited_body, now, edited_by, outgoing_id),
        )
    else:
        cur = conn.execute(
            "UPDATE outgoing_messages "
            "SET status='ready', approved_at=? "
            "WHERE id = ? AND status = 'needs_review'",
            (now, outgoing_id),
        )
    return cur.rowcount > 0


def reject_outgoing(outgoing_id: str, notes: Optional[str] = None) -> bool:
    cur = get_db().execute(
        "UPDATE outgoing_messages "
        "SET status='rejected', rejected_at=?, notes=COALESCE(?, notes) "
        "WHERE id = ? AND status IN ('needs_review','ready')",
        (utcnow_iso(), notes, outgoing_id),
    )
    return cur.rowcount > 0


def claim_outgoing_for_sending(outgoing_id: str, attempt_id: Optional[str] = None) -> Optional[str]:
    """Atomically move a message from 'ready' to 'sending'. Only one worker can
    claim a given message; returns the attempt_id on success, None on miss.

    This is the core of the two-phase commit: the sender must have a unique
    attempt_id stamped on the row before any real IO happens. If the sender
    crashes mid-send, recover_stuck_sending() will release it. If the send
    succeeds, mark_outgoing_sent() closes the attempt.
    """
    attempt_id = attempt_id or uuid.uuid4().hex
    cur = get_db().execute(
        "UPDATE outgoing_messages "
        "SET status='sending', attempt_id=?, claimed_at=?, send_attempts = send_attempts + 1 "
        "WHERE id = ? AND status = 'ready'",
        (attempt_id, utcnow_iso(), outgoing_id),
    )
    if cur.rowcount == 0:
        return None
    return attempt_id


def claim_next_ready(channel: str) -> Optional[tuple[dict, str]]:
    """Pick the oldest 'ready' message for a channel and atomically claim it.
    Returns (message_dict, attempt_id) or None.
    """
    conn = get_db()
    with transaction():
        row = conn.execute(
            "SELECT id FROM outgoing_messages WHERE status='ready' AND channel=? "
            "ORDER BY created_at ASC LIMIT 1",
            (channel,),
        ).fetchone()
        if row is None:
            return None
        attempt_id = uuid.uuid4().hex
        cur = conn.execute(
            "UPDATE outgoing_messages "
            "SET status='sending', attempt_id=?, claimed_at=?, send_attempts = send_attempts + 1 "
            "WHERE id = ? AND status = 'ready'",
            (attempt_id, utcnow_iso(), row["id"]),
        )
        if cur.rowcount == 0:
            # Someone else grabbed it between SELECT and UPDATE.
            return None
    msg = get_outgoing(row["id"])
    return (msg, attempt_id) if msg else None


def mark_outgoing_sent(outgoing_id: str, attempt_id: str,
                       channel_message_id: Optional[str] = None) -> bool:
    """Close a successful send attempt. Only succeeds if attempt_id still
    matches — protects against stale callers whose attempt was already
    recovered."""
    cur = get_db().execute(
        "UPDATE outgoing_messages "
        "SET status='sent', sent_at=?, channel_message_id=?, attempt_id=NULL, send_error=NULL "
        "WHERE id = ? AND status='sending' AND attempt_id = ?",
        (utcnow_iso(), channel_message_id, outgoing_id, attempt_id),
    )
    return cur.rowcount > 0


def mark_outgoing_failed(outgoing_id: str, attempt_id: str, error: str,
                         requeue: bool = False) -> bool:
    """Close a failed send attempt. If requeue=True, move back to 'ready' for
    a later retry; otherwise move to 'failed' for human review."""
    new_status = "ready" if requeue else "failed"
    cur = get_db().execute(
        "UPDATE outgoing_messages "
        "SET status=?, send_error=?, attempt_id=NULL "
        "WHERE id = ? AND status='sending' AND attempt_id = ?",
        (new_status, error, outgoing_id, attempt_id),
    )
    return cur.rowcount > 0


def recover_stuck_sending(older_than_sec: int = 300) -> int:
    """Called at sender startup and periodically. Any 'sending' row older than
    `older_than_sec` is moved back to 'ready' (the prior worker crashed before
    finishing). Returns the number of rows recovered.

    ВАЖНО (P1-NEW-2): `retry_count` инкрементируется — иначе recovered-записи
    могут бесконечно переклеймиваться, и MAX_RETRIES никогда не срабатывает.
    Если после инкремента достигнут MAX — запись уходит в 'failed' с
    эскалацией (так же, как в mark_outgoing_failed_with_backoff).

    The attempt_id is cleared so any zombie caller that eventually comes back
    with the old attempt_id gets rejected by mark_outgoing_sent/failed.
    """
    conn = get_db()
    threshold = utcnow_iso()
    cutoff_epoch = time.time() - older_than_sec
    cutoff_iso = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Сначала собираем кандидатов (чтобы знать, кого эскалировать)
    stuck = conn.execute(
        "SELECT id, retry_count, conversation_id, job_id, developer_id, "
        "       channel, recipient, send_error "
        "FROM outgoing_messages "
        "WHERE status='sending' AND (claimed_at IS NULL OR claimed_at < ?)",
        (cutoff_iso,),
    ).fetchall()

    recovered = 0
    for r in stuck:
        tried = (r["retry_count"] or 0) + 1
        err_note = f" [recovered-stuck-sending@{threshold}]"
        if tried >= MAX_RETRIES:
            # Исчерпали — уходим в 'failed' + эскалация.
            cur = conn.execute(
                "UPDATE outgoing_messages "
                "SET status='failed', attempt_id=NULL, retry_count=?, "
                "    send_error=COALESCE(send_error,'') || ? "
                "WHERE id=? AND status='sending'",
                (tried, err_note + " [exhausted]", r["id"]),
            )
            if cur.rowcount > 0:
                try:
                    insert_escalation({
                        "conversation_id": r["conversation_id"],
                        "job_id": r["job_id"],
                        "developer_id": r["developer_id"],
                        "channel": r["channel"],
                        "incoming_message": None,
                        "reason": f"outgoing stuck and exhausted after {tried} attempts",
                        "suggested_human_action": (
                            f"Запись {r['id']} зависла в 'sending' и после recovery "
                            f"достигла MAX_RETRIES. Проверь журнал, возможно перезапусти."
                        ),
                        "priority": "high",
                    })
                    notify_admin(
                        summary=f"outgoing stuck+exhausted → {r['recipient']}",
                        message=(
                            f"Запись {r['id']} ({r['channel']} → {r['recipient']}) "
                            f"зависла в 'sending' {tried} раз. Помечена 'failed'."
                        ),
                        urgency="high",
                        type_="outgoing_stuck_exhausted",
                        job_id=r["job_id"],
                        conversation_id=r["conversation_id"],
                    )
                except Exception as e:
                    print(f"[pf_db.recover_stuck_sending] escalation failed: {e}",
                          file=sys.stderr, flush=True)
        else:
            # Возвращаем в ready с backoff
            backoff = _RETRY_BACKOFF_SEC[min(tried - 1, len(_RETRY_BACKOFF_SEC) - 1)]
            cur = conn.execute(
                "UPDATE outgoing_messages "
                "SET status='ready', attempt_id=NULL, retry_count=?, next_retry_at=?, "
                "    send_error=COALESCE(send_error,'') || ? "
                "WHERE id=? AND status='sending'",
                (tried, _iso_offset(backoff), err_note, r["id"]),
            )
        recovered += cur.rowcount

    return recovered


# Экспоненциальный backoff между попытками retry. Значения подобраны
# консервативно: 5 мин → 15 → 60 — после трёх промахов имеет смысл отдавать
# человеку, а не дёргать SMTP каждый тик.
_RETRY_BACKOFF_SEC = (300, 900, 3600)
MAX_RETRIES = 3


def _iso_offset(seconds: int) -> str:
    t = time.time() + seconds
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mark_outgoing_failed_with_backoff(outgoing_id: str, attempt_id: str,
                                      error: str) -> tuple[bool, str]:
    """Failure path для транспортных демонов: если ещё есть попытки —
    возвращаем запись в 'ready' с next_retry_at = now + backoff. Если
    попытки исчерпаны — оставляем 'failed' И генерируем эскалацию +
    notify_admin (P1-NEW-4: раньше после 3 неудач сообщение тихо умирало).

    Возвращает (ok, new_status).
    """
    conn = get_db()
    row = conn.execute(
        "SELECT retry_count, conversation_id, job_id, developer_id, channel, recipient "
        "FROM outgoing_messages WHERE id=? AND status='sending' AND attempt_id=?",
        (outgoing_id, attempt_id),
    ).fetchone()
    if row is None:
        # Кто-то другой уже закрыл/перезахватил запись.
        return False, "stale"

    tried = (row["retry_count"] or 0) + 1
    if tried >= MAX_RETRIES:
        cur = conn.execute(
            "UPDATE outgoing_messages "
            "SET status='failed', send_error=?, attempt_id=NULL, retry_count=? "
            "WHERE id=? AND status='sending' AND attempt_id=?",
            (error, tried, outgoing_id, attempt_id),
        )
        if cur.rowcount > 0:
            # Эскалация после исчерпания попыток. Без неё оператор не узнаёт,
            # что отправка тихо ушла в навсегда-failed.
            try:
                insert_escalation({
                    "conversation_id": row["conversation_id"],
                    "job_id": row["job_id"],
                    "developer_id": row["developer_id"],
                    "channel": row["channel"],
                    "incoming_message": None,
                    "reason": f"outgoing failed permanently after {tried} attempts: {error}",
                    "suggested_human_action": (
                        f"Проверь причину: {error}. Можно изменить body через /review "
                        f"и перезапустить, либо отметить вакансию как rejected."
                    ),
                    "priority": "high",
                })
                notify_admin(
                    summary=f"outgoing failed permanently → {row['recipient']}",
                    message=(
                        f"Сообщение {outgoing_id} ({row['channel']} → {row['recipient']}) "
                        f"упало {tried} раза подряд.\nПоследняя ошибка: {error}\n"
                        f"Запись помечена 'failed', эскалация открыта."
                    ),
                    urgency="high",
                    type_="outgoing_exhausted",
                    job_id=row["job_id"],
                    conversation_id=row["conversation_id"],
                )
            except Exception as e:
                # Алерт сам упал — логируем в stderr файл логгера.
                print(f"[pf_db.mark_outgoing_failed_with_backoff] "
                      f"escalation/notify_admin failed: {e}",
                      file=sys.stderr, flush=True)
        return cur.rowcount > 0, "failed"

    backoff = _RETRY_BACKOFF_SEC[min(tried - 1, len(_RETRY_BACKOFF_SEC) - 1)]
    cur = conn.execute(
        "UPDATE outgoing_messages "
        "SET status='ready', send_error=?, attempt_id=NULL, "
        "    retry_count=?, next_retry_at=? "
        "WHERE id=? AND status='sending' AND attempt_id=?",
        (error, tried, _iso_offset(backoff), outgoing_id, attempt_id),
    )
    return cur.rowcount > 0, "ready"


def requeue_failed_for_retry() -> int:
    """Вернуть все failed outgoing, у которых retry_count < MAX и
    next_retry_at <= now, обратно в 'ready'. Вызывается периодически
    отправителями. Возвращает число перемещённых записей.
    """
    now = utcnow_iso()
    cur = get_db().execute(
        "UPDATE outgoing_messages "
        "SET status='ready', send_error=COALESCE(send_error,'') || ' [retry-requeue]' "
        "WHERE status='failed' AND retry_count < ? "
        "  AND (next_retry_at IS NULL OR next_retry_at <= ?)",
        (MAX_RETRIES, now),
    )
    return cur.rowcount


def requeue_failed_notifications() -> int:
    """Аналог для notifications. Любой network hiccup → telegram_notifier
    пометил failed → никто не повторит. После фикса — у notifications тоже
    есть retry_count/next_retry_at, и notifier возвращает их в 'pending'
    когда backoff истёк.
    """
    now = utcnow_iso()
    cur = get_db().execute(
        "UPDATE notifications "
        "SET telegram_status='pending' "
        "WHERE telegram_status='failed' AND retry_count < ? "
        "  AND (next_retry_at IS NULL OR next_retry_at <= ?)",
        (MAX_RETRIES, now),
    )
    return cur.rowcount


def mark_notification_failed_with_backoff(notif_id: str, error: str) -> tuple[bool, str]:
    """Failure path для telegram_notifier. Возвращает (ok, new_status).

    Успешный вызов `mark_notification_failed` сейчас помечает навсегда —
    эта обёртка планирует ретраи с backoff'ом.
    """
    row = get_db().execute(
        "SELECT retry_count FROM notifications WHERE id=? AND telegram_status='pending'",
        (notif_id,),
    ).fetchone()
    if row is None:
        return False, "stale"
    tried = (row["retry_count"] or 0) + 1
    if tried >= MAX_RETRIES:
        cur = get_db().execute(
            "UPDATE notifications "
            "SET telegram_status='failed', telegram_response=?, retry_count=? "
            "WHERE id=? AND telegram_status='pending'",
            (_j({"error": error}), tried, notif_id),
        )
        return cur.rowcount > 0, "failed"

    backoff = _RETRY_BACKOFF_SEC[min(tried - 1, len(_RETRY_BACKOFF_SEC) - 1)]
    cur = get_db().execute(
        "UPDATE notifications "
        "SET telegram_status='failed', telegram_response=?, "
        "    retry_count=?, next_retry_at=? "
        "WHERE id=? AND telegram_status='pending'",
        (_j({"error": error}), tried, _iso_offset(backoff), notif_id),
    )
    # Отметим как failed с будущим next_retry_at — requeue_failed_notifications
    # поднимет её обратно в pending, когда backoff истечёт.
    return cur.rowcount > 0, "failed_retry_scheduled"


def update_outgoing_body(outgoing_id: str, new_body: str, edited_by: str = "human") -> bool:
    """Edit the body of a not-yet-sent message (any pre-send state)."""
    cur = get_db().execute(
        "UPDATE outgoing_messages "
        "SET body = ?, edited_at = ?, edited_by = ? "
        "WHERE id = ? AND status IN ('needs_review','ready','failed')",
        (new_body, utcnow_iso(), edited_by, outgoing_id),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Notifications (outbound Telegram bot messages to the human operator)
# ---------------------------------------------------------------------------

def insert_notification(notif: dict) -> str:
    nid = notif.get("id") or new_id("notif")
    get_db().execute(
        """
        INSERT INTO notifications (
            id, created_at, type, urgency, job_id, job_title, conversation_id,
            outgoing_id, draft_file, escalation_file, reason, summary,
            recipient, telegram_chat_id, message_sent, telegram_status,
            test_entry
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            nid,
            notif.get("created_at") or utcnow_iso(),
            notif.get("type"),
            notif.get("urgency"),
            notif.get("job_id"),
            notif.get("job_title"),
            notif.get("conversation_id"),
            notif.get("outgoing_id"),
            notif.get("draft_file"),
            notif.get("escalation_file"),
            notif.get("reason"),
            notif.get("summary"),
            notif.get("recipient"),
            notif.get("telegram_chat_id"),
            notif.get("message_sent"),
            notif.get("telegram_status", "pending"),
            1 if notif.get("test_entry") else 0,
        ),
    )
    return nid


def list_pending_notifications(limit: int = 100) -> list[dict]:
    rows = get_db().execute(
        "SELECT * FROM notifications WHERE telegram_status='pending' "
        "ORDER BY created_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_notification_sent(notif_id: str, telegram_response: Optional[dict] = None) -> bool:
    cur = get_db().execute(
        "UPDATE notifications "
        "SET telegram_status='sent', sent_at=?, telegram_response=? "
        "WHERE id = ? AND telegram_status='pending'",
        (utcnow_iso(), _j(telegram_response), notif_id),
    )
    return cur.rowcount > 0


def mark_notification_failed(notif_id: str, error: str) -> bool:
    cur = get_db().execute(
        "UPDATE notifications "
        "SET telegram_status='failed', telegram_response=? "
        "WHERE id = ? AND telegram_status='pending'",
        (_j({"error": error}), notif_id),
    )
    return cur.rowcount > 0


def get_notification(notif_id: str) -> Optional[dict]:
    row = get_db().execute(
        "SELECT * FROM notifications WHERE id = ?", (notif_id,)
    ).fetchone()
    return _row_to_dict(row)


def notify_admin(summary: str, message: str,
                 urgency: str = "normal",
                 type_: str = "admin_alert",
                 job_id: Optional[str] = None,
                 conversation_id: Optional[str] = None) -> Optional[str]:
    """Записать системный алерт в notifications так, чтобы его реально
    смогли доставить. Сам читает notifications-config.json и подставляет
    telegram_chat_id первого получателя, у которого в `notify_on` есть
    `admin` (или `high`, если для `admin` никого нет).

    Почему это отдельная функция: раньше скиллы делали `insert_notification`
    напрямую, забывали поставить `recipient`/`telegram_chat_id` → notifier
    получал запись с `chat_id=None`, сразу помечал её failed. Человек про
    проблему не узнавал (см. notif-de689e5b в corrupted-DB от 2026-04-16).

    Возвращает id записи или None, если не удалось загрузить конфиг.
    """
    try:
        # Импорт внутри функции — pf_secrets тянет config-папку,
        # для юнит-тестов pf_db без конфигов не обязателен.
        import pf_secrets  # type: ignore
        cfg = pf_secrets.load_config("notifications-config.json")
    except Exception as e:
        print(f"[pf_db.notify_admin] cannot load notifications-config: {e}",
              file=sys.stderr, flush=True)
        cfg = {}

    recipients = cfg.get("recipients") or []
    chat_id: Optional[str] = None
    # Приоритет 1 — явный флаг is_admin: true в схеме recipient.
    for r in recipients:
        if r.get("is_admin") is True:
            cid = r.get("telegram_chat_id")
            if cid and "PASTE" not in str(cid):
                chat_id = cid
                break
    # Приоритет 2 — back-compat: notify_on содержит "admin"/"HIGH"/"high".
    if chat_id is None:
        for r in recipients:
            notify_on = [str(x).lower() for x in (r.get("notify_on") or [])]
            if "admin" in notify_on or "high" in notify_on:
                cid = r.get("telegram_chat_id")
                if cid and "PASTE" not in str(cid):
                    chat_id = cid
                    break
    # Fallback — первый попавшийся реальный chat_id.
    if chat_id is None:
        for r in recipients:
            cid = r.get("telegram_chat_id")
            if cid and "PASTE" not in str(cid):
                chat_id = cid
                break

    if not chat_id:
        # P1-NEW-5: file-fallback. Если Telegram-получателя нет — пишем в
        # alerts.log (рядом с projectfinder.log), чтобы критичные алерты
        # хотя бы не терялись. Это не панацея, но при «БД повреждена» или
        # «outgoing exhausted» оператор увидит файл при следующем входе.
        try:
            alerts_log = PROJECT_ROOT / "logs" / "alerts.log"
            alerts_log.parent.mkdir(parents=True, exist_ok=True)
            with alerts_log.open("a", encoding="utf-8") as f:
                f.write(
                    f"[{utcnow_iso()}] [{urgency}] {type_}: {summary}\n"
                    f"  job_id={job_id} conv={conversation_id}\n"
                    f"  {message}\n\n"
                )
        except Exception as e:
            print(f"[pf_db.notify_admin] alerts.log write failed: {e}",
                  file=sys.stderr, flush=True)

        # Всё равно пишем запись в БД — трейс, что алерт был сгенерирован.
        # Помечаем failed, чтобы notifier не пытался её послать в цикле.
        nid = insert_notification({
            "type": type_,
            "urgency": urgency,
            "summary": summary,
            "message_sent": message,
            "recipient": "admin",
            "telegram_chat_id": None,
            "job_id": job_id,
            "conversation_id": conversation_id,
            "telegram_status": "failed",
            "telegram_response": _j({"error": "notify_admin: no admin chat_id configured; written to logs/alerts.log"}),
        })
        print(f"[pf_db.notify_admin] no admin chat_id; logged to alerts.log + failed notif {nid}",
              file=sys.stderr, flush=True)
        return nid

    return insert_notification({
        "type": type_,
        "urgency": urgency,
        "summary": summary,
        "message_sent": message,
        "recipient": "admin",
        "telegram_chat_id": chat_id,
        "job_id": job_id,
        "conversation_id": conversation_id,
    })


def ack_notification(notif_id: str) -> bool:
    cur = get_db().execute(
        "UPDATE notifications SET acknowledged=1, acknowledged_at=? "
        "WHERE id = ? AND acknowledged = 0",
        (utcnow_iso(), notif_id),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Escalations
# ---------------------------------------------------------------------------

def insert_escalation(esc: dict) -> str:
    eid = esc.get("id") or new_id("esc")
    get_db().execute(
        """
        INSERT INTO escalations (
            id, conversation_id, job_id, developer_id, channel, created_at,
            incoming_message, reason, suggested_human_action, status, priority
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            eid,
            esc.get("conversation_id"),
            esc.get("job_id"),
            esc.get("developer_id"),
            esc.get("channel"),
            esc.get("created_at") or utcnow_iso(),
            esc.get("incoming_message"),
            esc.get("reason"),
            esc.get("suggested_human_action"),
            esc.get("status", "open"),
            esc.get("priority"),
        ),
    )
    return eid


def list_open_escalations() -> list[dict]:
    rows = get_db().execute(
        "SELECT * FROM escalations WHERE status='open' ORDER BY created_at ASC"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def resolve_escalation(esc_id: str, status: str = "resolved") -> bool:
    cur = get_db().execute(
        "UPDATE escalations SET status=? WHERE id=? AND status='open'",
        (status, esc_id),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Service key-value state (replaces email_io_state.json, bot_handler_state.json)
# ---------------------------------------------------------------------------

def state_get(key: str, default: Any = None) -> Any:
    row = get_db().execute(
        "SELECT value_json FROM service_state WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        return default
    return _unj(row["value_json"])


def state_set(key: str, value: Any) -> None:
    """Сохранить значение под ключом. value=None означает «снять» — DELETE.

    `service_state.value_json` имеет NOT NULL constraint, поэтому INSERT
    с None упадёт IntegrityError. Семантически value=None всегда означает
    «забудь это» (например, advisory-lock снимается через `value=None`),
    поэтому преобразуем в DELETE.
    """
    if value is None:
        get_db().execute("DELETE FROM service_state WHERE key=?", (key,))
        return
    get_db().execute(
        """
        INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at = excluded.updated_at
        """,
        (key, _j(value), utcnow_iso()),
    )


def state_update(key: str, mutator, default: Any = None) -> Any:
    """Atomic read-modify-write on a single key. Runs inside BEGIN IMMEDIATE."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT value_json FROM service_state WHERE key=?", (key,)
        ).fetchone()
        current = _unj(row["value_json"]) if row else default
        if current is None and default is not None:
            current = default
        new_value = mutator(current)
        conn.execute(
            """
            INSERT INTO service_state (key, value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, _j(new_value), utcnow_iso()),
        )
        return new_value


# ---------------------------------------------------------------------------
# Seen message ids (cross-source dedup for scanners)
# ---------------------------------------------------------------------------

def mark_seen(source: str, external_id: str) -> bool:
    """Record that we've seen this external id. Returns True if it's new,
    False if we'd already seen it."""
    cur = get_db().execute(
        "INSERT OR IGNORE INTO seen_message_ids (source, external_id, seen_at) VALUES (?, ?, ?)",
        (source, external_id, utcnow_iso()),
    )
    return cur.rowcount > 0


def has_seen(source: str, external_id: str) -> bool:
    row = get_db().execute(
        "SELECT 1 FROM seen_message_ids WHERE source=? AND external_id=?",
        (source, external_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Bulk helpers for debugging / admin
# ---------------------------------------------------------------------------

def counts() -> dict:
    """Snapshot counters — handy for /status command and tests."""
    conn = get_db()

    def n(sql: str, params: tuple = ()) -> int:
        return conn.execute(sql, params).fetchone()[0]

    return {
        "jobs":              n("SELECT COUNT(*) FROM jobs"),
        "jobs_matched":      n("SELECT COUNT(*) FROM jobs WHERE status='matched'"),
        "conversations":     n("SELECT COUNT(*) FROM conversations"),
        "conv_active":       n("SELECT COUNT(*) FROM conversations WHERE status='active'"),
        "incoming_new":      n("SELECT COUNT(*) FROM incoming_messages WHERE status='new'"),
        "outgoing_ready":    n("SELECT COUNT(*) FROM outgoing_messages WHERE status='ready'"),
        "outgoing_sending":  n("SELECT COUNT(*) FROM outgoing_messages WHERE status='sending'"),
        "outgoing_sent":     n("SELECT COUNT(*) FROM outgoing_messages WHERE status='sent'"),
        "outgoing_failed":   n("SELECT COUNT(*) FROM outgoing_messages WHERE status='failed'"),
        "outgoing_needs_review": n("SELECT COUNT(*) FROM outgoing_messages WHERE status='needs_review'"),
        "notifications_pending": n("SELECT COUNT(*) FROM notifications WHERE telegram_status='pending'"),
        "escalations_open":  n("SELECT COUNT(*) FROM escalations WHERE status='open'"),
    }


if __name__ == "__main__":
    # Smoke test: create schema, show counts, exit.
    init_db()
    print(f"DB: {DB_PATH}")
    print("Schema initialised. Counts:")
    for k, v in counts().items():
        print(f"  {k:28s} {v}")
