"""Idempotent dedup table with the claim/commit cycle (ADR-0093, ramjac ADR-0014).

The cycle is: CLAIM the dedupKey → RUN the handler → COMMIT the key.
Never mark-then-process: recording the key before the handler runs loses a
message on a mid-handler crash (the exact bug this module exists to prevent).

Provenance: the claim/commit pattern is ported from ramjac ADR-0014.
A later ramjac fix should verify that the SQLite semantics here remain
compatible with that ADR's durable-store guarantees.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path


class ClaimResult(str, Enum):
    GRANTED = "granted"
    DUPLICATE = "duplicate"
    CLAIMED_ELSEWHERE = "claimed-elsewhere"


class DedupBackend:
    """SQLite dedup table scoped to a consumer_id.

    Dedup is keyed on (dedup_key, consumer_id) so two distinct consumers
    processing the same logical event each get one delivery rather than
    sharing a single slot (see test_unscoped_dedup_collision).

    Args:
        db_path: Filesystem path to the SQLite file. Tests pass a tmp path.
        consumer_id: Label for this consumer. Scopes dedup entries.
        crash_window: How long a claimed key stays reserved before it is
            considered expired and re-claimable. Pass timedelta(0) in tests
            to make claims expire immediately after creation.
    """

    def __init__(
        self,
        db_path: str | Path,
        consumer_id: str,
    ) -> None:
        self._db_path = str(db_path)
        self._consumer_id = consumer_id
        self._init_db()

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
                CREATE TABLE IF NOT EXISTS dedup_keys (
                    dedup_key    TEXT NOT NULL,
                    consumer_id  TEXT NOT NULL,
                    state        TEXT NOT NULL CHECK(state IN ('claimed', 'committed')),
                    claimed_at   TEXT NOT NULL,
                    expires_at   TEXT NOT NULL,
                    PRIMARY KEY (dedup_key, consumer_id)
                )
                """
            )
            conn.commit()

    def claim(
        self, dedup_key: str, now: datetime, crash_window: timedelta
    ) -> ClaimResult:
        """Atomically attempt to claim dedupKey for this consumer.

        Returns:
            GRANTED — the claim is now held by this consumer.
            DUPLICATE — the key was already committed; drop the message.
            CLAIMED_ELSEWHERE — another (unexpired) claim exists; back off.

        A claimed entry whose expires_at has passed is re-claimable: the
        previous claimant died mid-handler without committing.

        Provenance: claim/commit cycle from ramjac ADR-0014.
        """
        expires_at = now + crash_window
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT state, expires_at FROM dedup_keys
                WHERE dedup_key = ? AND consumer_id = ?
                """,
                (dedup_key, self._consumer_id),
            ).fetchone()

            if row is not None:
                if row["state"] == "committed":
                    return ClaimResult.DUPLICATE

                # state == "claimed" — check whether the crash window has passed.
                if datetime.fromisoformat(row["expires_at"]) > now:
                    return ClaimResult.CLAIMED_ELSEWHERE

                # Expired claim: previous claimant died before commit. Re-claim.
                conn.execute(
                    """
                    UPDATE dedup_keys
                    SET state = 'claimed', claimed_at = ?, expires_at = ?
                    WHERE dedup_key = ? AND consumer_id = ?
                    """,
                    (
                        now.isoformat(),
                        expires_at.isoformat(),
                        dedup_key,
                        self._consumer_id,
                    ),
                )
                conn.commit()
                return ClaimResult.GRANTED

            conn.execute(
                """
                INSERT INTO dedup_keys (dedup_key, consumer_id, state, claimed_at, expires_at)
                VALUES (?, ?, 'claimed', ?, ?)
                """,
                (
                    dedup_key,
                    self._consumer_id,
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            conn.commit()
            return ClaimResult.GRANTED

    def commit(self, dedup_key: str) -> None:
        """Mark dedupKey as committed (terminal). No further deliveries."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE dedup_keys SET state = 'committed'
                WHERE dedup_key = ? AND consumer_id = ?
                """,
                (dedup_key, self._consumer_id),
            )
            conn.commit()
