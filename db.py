"""SQLite storage for TeraBox cookies, targets, uploads, batch jobs and sessions."""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

import config

_DB_PATH = Path(
    os.getenv("BOT_DB_PATH", str(config.DATA_DIR / "bot.sqlite3"))
).expanduser()
_cipher = Fernet(config.ENCRYPTION_KEY.encode("ascii"))
_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS terabox_tokens (
    user_id INTEGER PRIMARY KEY,
    token BLOB NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    username TEXT,
    chat_type TEXT,
    start_message_id INTEGER NOT NULL,
    start_link TEXT,
    auto_mode INTEGER NOT NULL DEFAULT 0,
    last_seen_id INTEGER NOT NULL DEFAULT 0,
    total_messages INTEGER NOT NULL DEFAULT 0,
    uploaded_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    target_id INTEGER,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    terabox_url TEXT NOT NULL,
    source_link TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_uploads_target ON uploads(target_id, id);

CREATE TABLE IF NOT EXISTS batch_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    start_id INTEGER NOT NULL,
    end_id INTEGER NOT NULL,
    cursor_id INTEGER NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    uploaded INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_batch_status ON batch_jobs(status, id);

CREATE TABLE IF NOT EXISTS user_sessions (
    user_id INTEGER PRIMARY KEY,
    session BLOB NOT NULL,
    phone TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    bot_username TEXT,
    display_name TEXT,
    target_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, bot_id)
);

CREATE INDEX IF NOT EXISTS idx_bot_sources_user ON bot_sources(user_id);
"""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        os.chmod(_DB_PATH, 0o600)
    except OSError:
        pass
    return conn


@contextmanager
def _connection():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _encrypt(value: str) -> bytes:
    return _cipher.encrypt(value.encode("utf-8"))


def _decrypt(blob: bytes) -> str | None:
    try:
        return _cipher.decrypt(bytes(blob)).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def init_schema() -> None:
    with _connection() as conn:
        conn.executescript(_SCHEMA)
        # Column added after the first release; migrate existing installs.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(targets)")}
        if "bot_watch" not in columns:
            conn.execute(
                "ALTER TABLE targets ADD COLUMN bot_watch INTEGER NOT NULL DEFAULT 0"
            )


# ---------------------------------------------------------------- TeraBox token
def get_token(user_id: int) -> str | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT token FROM terabox_tokens WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    if row is None:
        return None
    token = _decrypt(row["token"])
    if token is None:
        # A restored DB with the wrong encryption key cannot be used safely.
        delete_token(user_id)
    return token


def save_token(user_id: int, token: str) -> None:
    with _connection() as conn:
        conn.execute(
            "INSERT INTO terabox_tokens (user_id, token, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id) DO UPDATE SET token=excluded.token, "
            "updated_at=CURRENT_TIMESTAMP",
            (int(user_id), _encrypt(token)),
        )


def delete_token(user_id: int) -> bool:
    with _connection() as conn:
        cur = conn.execute(
            "DELETE FROM terabox_tokens WHERE user_id = ?", (int(user_id),)
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------- targets
def add_target(
    user_id: int,
    chat_id: int,
    title: str,
    username: str | None,
    chat_type: str | None,
    start_message_id: int,
    start_link: str | None,
    bot_watch: int = 0,
) -> int:
    with _connection() as conn:
        conn.execute(
            "INSERT INTO targets (user_id, chat_id, title, username, chat_type, "
            "start_message_id, start_link, last_seen_id, bot_watch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, chat_id) DO UPDATE SET "
            "title=excluded.title, username=excluded.username, "
            "chat_type=excluded.chat_type, "
            "start_message_id=excluded.start_message_id, "
            "start_link=excluded.start_link, "
            "last_seen_id=excluded.last_seen_id, "
            "bot_watch=excluded.bot_watch",
            (
                int(user_id),
                int(chat_id),
                title,
                username,
                chat_type,
                int(start_message_id),
                start_link,
                max(0, int(start_message_id) - 1),
                1 if bot_watch else 0,
            ),
        )
        row = conn.execute(
            "SELECT id FROM targets WHERE user_id = ? AND chat_id = ?",
            (int(user_id), int(chat_id)),
        ).fetchone()
    return int(row["id"])


def get_target(target_id: int) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM targets WHERE id = ?", (int(target_id),)
        ).fetchone()
    return _row(row)


def get_target_by_chat(user_id: int, chat_id: int) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM targets WHERE user_id = ? AND chat_id = ?",
            (int(user_id), int(chat_id)),
        ).fetchone()
    return _row(row)


def list_targets(user_id: int) -> list[dict[str, Any]]:
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM targets WHERE user_id = ? ORDER BY id ASC", (int(user_id),)
        ).fetchall()
    return _rows(rows)


def list_auto_targets() -> list[dict[str, Any]]:
    """Targets polled by the auto engine: auto mode ON or bot-watch targets."""
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM targets WHERE auto_mode = 1 OR bot_watch = 1 ORDER BY id ASC"
        ).fetchall()
    return _rows(rows)


def delete_target(target_id: int) -> bool:
    with _connection() as conn:
        conn.execute("DELETE FROM uploads WHERE target_id = ?", (int(target_id),))
        conn.execute("DELETE FROM batch_jobs WHERE target_id = ?", (int(target_id),))
        cur = conn.execute("DELETE FROM targets WHERE id = ?", (int(target_id),))
        return cur.rowcount > 0


def set_auto_mode(target_id: int, enabled: bool) -> None:
    with _connection() as conn:
        conn.execute(
            "UPDATE targets SET auto_mode = ? WHERE id = ?",
            (1 if enabled else 0, int(target_id)),
        )


def set_last_seen(target_id: int, message_id: int) -> None:
    with _connection() as conn:
        conn.execute(
            "UPDATE targets SET last_seen_id = MAX(last_seen_id, ?) WHERE id = ?",
            (int(message_id), int(target_id)),
        )


def set_target_total(target_id: int, total: int) -> None:
    with _connection() as conn:
        conn.execute(
            "UPDATE targets SET total_messages = ? WHERE id = ?",
            (max(0, int(total)), int(target_id)),
        )


def bump_target(
    target_id: int,
    *,
    uploaded: int = 0,
    failed: int = 0,
    skipped: int = 0,
) -> None:
    with _connection() as conn:
        conn.execute(
            "UPDATE targets SET uploaded_count = uploaded_count + ?, "
            "failed_count = failed_count + ?, skipped_count = skipped_count + ? "
            "WHERE id = ?",
            (int(uploaded), int(failed), int(skipped), int(target_id)),
        )


# --------------------------------------------------------------------- uploads
def upload_exists(user_id: int, chat_id: int, message_id: int) -> str | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT terabox_url FROM uploads "
            "WHERE user_id = ? AND chat_id = ? AND message_id = ?",
            (int(user_id), int(chat_id), int(message_id)),
        ).fetchone()
    return row["terabox_url"] if row else None


def save_upload(
    user_id: int,
    target_id: int | None,
    chat_id: int,
    message_id: int,
    file_name: str,
    file_size: int,
    terabox_url: str,
    source_link: str | None,
) -> bool:
    with _connection() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO uploads (user_id, target_id, chat_id, message_id, "
            "file_name, file_size, terabox_url, source_link) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(user_id),
                int(target_id) if target_id else None,
                int(chat_id),
                int(message_id),
                file_name,
                int(file_size),
                terabox_url,
                source_link,
            ),
        )
        return cur.rowcount > 0


def list_uploads(target_id: int, offset: int = 0, limit: int = 10) -> list[dict[str, Any]]:
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM uploads WHERE target_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
            (int(target_id), int(limit), int(offset)),
        ).fetchall()
    return _rows(rows)


def count_uploads(target_id: int) -> int:
    with _connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM uploads WHERE target_id = ?", (int(target_id),)
        ).fetchone()
    return int(row["n"]) if row else 0


# ------------------------------------------------------------------ batch jobs
def create_batch_job(
    user_id: int,
    target_id: int,
    start_id: int,
    end_id: int,
    total: int,
) -> int:
    with _connection() as conn:
        cur = conn.execute(
            "INSERT INTO batch_jobs (user_id, target_id, start_id, end_id, cursor_id, "
            "total, status) VALUES (?, ?, ?, ?, ?, ?, 'running')",
            (
                int(user_id),
                int(target_id),
                int(start_id),
                int(end_id),
                int(start_id) - 1,
                int(total),
            ),
        )
        return int(cur.lastrowid)


def get_batch_job(job_id: int) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE id = ?", (int(job_id),)
        ).fetchone()
    return _row(row)


def update_batch_job(job_id: int, **fields: Any) -> None:
    allowed = {
        "cursor_id",
        "processed",
        "uploaded",
        "failed",
        "skipped",
        "status",
        "error",
        "end_id",
        "total",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [int(job_id)]
    with _connection() as conn:
        conn.execute(
            f"UPDATE batch_jobs SET {assignments}, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            values,
        )


def list_resumable_jobs() -> list[dict[str, Any]]:
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM batch_jobs WHERE status IN ('running', 'paused') "
            "AND cursor_id < end_id ORDER BY id ASC"
        ).fetchall()
    return _rows(rows)


def latest_job_for_target(target_id: int) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE target_id = ? ORDER BY id DESC LIMIT 1",
            (int(target_id),),
        ).fetchone()
    return _row(row)


def get_running_batch_for_target(
    user_id: int, target_id: int
) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE user_id = ? AND target_id = ? "
            "AND status = 'running' ORDER BY id DESC LIMIT 1",
            (int(user_id), int(target_id)),
        ).fetchone()
    return _row(row)


def mark_stale_jobs_paused() -> int:
    with _connection() as conn:
        cur = conn.execute(
            "UPDATE batch_jobs SET status = 'paused', updated_at = CURRENT_TIMESTAMP "
            "WHERE status = 'running'"
        )
        return cur.rowcount


# ---------------------------------------------------------------- bot sources
def add_bot_source(
    user_id: int,
    bot_id: int,
    bot_username: str | None,
    display_name: str,
    target_id: int,
) -> None:
    with _connection() as conn:
        conn.execute(
            "INSERT INTO bot_sources (user_id, bot_id, bot_username, display_name, target_id) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, bot_id) DO UPDATE SET "
            "bot_username=excluded.bot_username, display_name=excluded.display_name, "
            "target_id=excluded.target_id",
            (
                int(user_id),
                int(bot_id),
                bot_username,
                display_name,
                int(target_id),
            ),
        )


def list_bot_sources(user_id: int) -> list[dict[str, Any]]:
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_sources WHERE user_id = ? ORDER BY id ASC",
            (int(user_id),),
        ).fetchall()
    return _rows(rows)


def find_bot_source(user_id: int, ref) -> dict[str, Any] | None:
    """Find a watched bot by numeric ID or username."""
    value = (str(ref).strip().lstrip("@")).lower()
    if not value:
        return None
    with _connection() as conn:
        row = None
        if value.isdigit():
            row = conn.execute(
                "SELECT * FROM bot_sources WHERE user_id = ? AND bot_id = ?",
                (int(user_id), int(value)),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM bot_sources WHERE user_id = ? "
                "AND LOWER(bot_username) = ?",
                (int(user_id), value),
            ).fetchone()
    return _row(row)


def get_bot_source_by_target(target_id: int) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM bot_sources WHERE target_id = ?", (int(target_id),)
        ).fetchone()
    return _row(row)


def delete_bot_source(source_id: int) -> bool:
    with _connection() as conn:
        cur = conn.execute(
            "DELETE FROM bot_sources WHERE id = ?", (int(source_id),)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------- user session
def save_session(user_id: int, session: str, phone: str | None = None) -> None:
    with _connection() as conn:
        conn.execute(
            "INSERT INTO user_sessions (user_id, session, phone, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id) DO UPDATE SET session=excluded.session, "
            "phone=excluded.phone, updated_at=CURRENT_TIMESTAMP",
            (int(user_id), _encrypt(session), phone),
        )


def get_session(user_id: int) -> str | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT session FROM user_sessions WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    if row is None:
        return None
    session = _decrypt(row["session"])
    if session is None:
        delete_session(user_id)
    return session


def get_session_phone(user_id: int) -> str | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT phone FROM user_sessions WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    return row["phone"] if row else None


def delete_session(user_id: int) -> bool:
    with _connection() as conn:
        cur = conn.execute(
            "DELETE FROM user_sessions WHERE user_id = ?", (int(user_id),)
        )
        return cur.rowcount > 0


init_schema()
