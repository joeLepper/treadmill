"""SQLite-backed durable outbox (ADR-0094).

Provides a local outbox that survives process restarts and network isolation.
The DB is opened in WAL mode so readers do not block writers.

ADR-0094 Decision #1 — atomic write: the outbox table lives in the SAME
SQLite database as the caller's state tables. write() accepts a caller-supplied
connection and inserts without committing. The caller owns commit/rollback, so
the outbox event and the caller's state change are ONE atomic transaction.
Use connect() to get a connection to this database for the caller's own
state-change transaction.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class OutboxRow:
    row_id: int
    dedup_key: str
    ordering_key: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
    published_at: datetime | None


class OutboxBackend:
    """Append-only outbox backed by a SQLite file.

    Args:
        db_path: Filesystem path to the SQLite file. Tests pass a tmp path.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        """Return a new connection to the outbox database.

        Callers use this to obtain a connection for atomic write()+state-change
        transactions — open a connection, do your state writes, call write(conn),
        then commit (or rollback) to make the event and state atomic.
        The caller owns the returned connection and must close it.
        """
        return self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedup_key     TEXT NOT NULL,
                    ordering_key  TEXT NOT NULL,
                    event_type    TEXT NOT NULL,
                    payload       TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    published_at  TEXT
                )
                """
            )
            conn.commit()

    def write(self, message: dict[str, Any], conn: sqlite3.Connection) -> None:
        """Append a message to the outbox on the caller's connection.

        The INSERT is executed on `conn` without committing. The caller owns
        the transaction: commit or rollback applies atomically to both this
        event and the caller's state change (ADR-0094 Decision #1 — no
        dual-write gap between state and event).
        """
        conn.execute(
            """
            INSERT INTO outbox
                (dedup_key, ordering_key, event_type, payload, created_at, published_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                message["dedupKey"],
                message["ordering_key"],
                message["event_type"],
                json.dumps(message["payload"]),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def read_pending(self, limit: int = 100) -> list[OutboxRow]:
        """Return pending rows (published_at IS NULL) in append order."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, dedup_key, ordering_key, event_type,
                       payload, created_at, published_at
                FROM outbox
                WHERE published_at IS NULL
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            OutboxRow(
                row_id=r["id"],
                dedup_key=r["dedup_key"],
                ordering_key=r["ordering_key"],
                event_type=r["event_type"],
                payload=json.loads(r["payload"]),
                created_at=datetime.fromisoformat(r["created_at"]),
                published_at=(
                    datetime.fromisoformat(r["published_at"])
                    if r["published_at"]
                    else None
                ),
            )
            for r in rows
        ]

    def mark_published(self, row_id: int) -> None:
        """Set published_at on the given row."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE outbox SET published_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), row_id),
            )
            conn.commit()
