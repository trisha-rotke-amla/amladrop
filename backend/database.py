"""
SQLite storage layer. Both the SMTP receiver process and the API process
open the same DB file. SQLite handles the concurrent access fine at the
volume a disposable-mail box sees (WAL mode + short transactions).
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

import config

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_conn():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mailbox TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                body_text TEXT,
                body_html TEXT,
                attachments TEXT NOT NULL DEFAULT '[]',
                received_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mailbox ON messages(mailbox)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_received_at ON messages(received_at)"
        )


def insert_message(mailbox, sender, recipient, subject, body_text, body_html, attachments):
    """attachments: list of {filename, content_type, size, data_b64}"""
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO messages
                (mailbox, sender, recipient, subject, body_text, body_html, attachments, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mailbox,
                sender,
                recipient,
                subject,
                body_text,
                body_html,
                json.dumps(attachments),
                now,
            ),
        )
        # Enforce per-mailbox cap: drop oldest beyond the limit.
        rows = conn.execute(
            "SELECT id FROM messages WHERE mailbox = ? ORDER BY received_at DESC",
            (mailbox,),
        ).fetchall()
        if len(rows) > config.MAX_MESSAGES_PER_MAILBOX:
            overflow_ids = [r["id"] for r in rows[config.MAX_MESSAGES_PER_MAILBOX:]]
            conn.executemany("DELETE FROM messages WHERE id = ?", [(i,) for i in overflow_ids])


def list_messages(mailbox):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, sender, subject, received_at,
                   substr(coalesce(body_text, ''), 1, 140) AS snippet
            FROM messages
            WHERE mailbox = ?
            ORDER BY received_at DESC
            """,
            (mailbox,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_message(message_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["attachments"] = json.loads(data["attachments"])
        return data


def delete_message(message_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))


def clear_mailbox(mailbox):
    with get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE mailbox = ?", (mailbox,))


def delete_expired(retention_hours):
    cutoff = time.time() - retention_hours * 3600
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM messages WHERE received_at < ?", (cutoff,))
        return cur.rowcount
