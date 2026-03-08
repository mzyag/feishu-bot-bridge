from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("CANCELSHIELD_DB", DATA_DIR / "cancelshield.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, ddl: str, column: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS team_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, email),
                FOREIGN KEY(team_id) REFERENCES teams(id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                label TEXT NOT NULL DEFAULT 'default',
                role TEXT NOT NULL DEFAULT 'editor',
                created_by_email TEXT,
                key_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                revoked_at TEXT,
                FOREIGN KEY(team_id) REFERENCES teams(id)
            )
            """
        )
        _add_column_if_missing(conn, "api_keys", "role TEXT NOT NULL DEFAULT 'editor'", "role")
        _add_column_if_missing(conn, "api_keys", "created_by_email TEXT", "created_by_email")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER,
                team_name TEXT NOT NULL,
                vendor TEXT NOT NULL,
                plan_name TEXT,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USD',
                renewal_date TEXT NOT NULL,
                owner_email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'trial',
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(team_id) REFERENCES teams(id)
            )
            """
        )

        _add_column_if_missing(conn, "subscriptions", "team_id INTEGER", "team_id")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_team_renewal ON subscriptions(team_id, renewal_date)"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                page_url TEXT,
                screenshot_path TEXT,
                details TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                reminder_date TEXT NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(subscription_id, reminder_date, channel),
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                webhook_url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, provider),
                FOREIGN KEY(team_id) REFERENCES teams(id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(team_id) REFERENCES teams(id)
            )
            """
        )
