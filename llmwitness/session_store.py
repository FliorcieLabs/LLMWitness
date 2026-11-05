"""SQLite persistence for unsealed ingestion sessions.

The ingestion service keeps sessions in memory for speed. This store mirrors
every accepted event to a local SQLite file so that a restart does not lose
runs that have not been sealed yet. Sealed sessions are removed from the store
because their receipt file is the durable record.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

STREAMS = ("sdk_events", "gateway_events", "extension_events")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    correlation_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL REFERENCES sessions(correlation_id) ON DELETE CASCADE,
    stream TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_session ON events(correlation_id, id);
"""


class SessionStore:
    """Write-through store for sessions that are still accepting events."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        try:
            # Scrubbed telemetry is still private to the user running the service.
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        # WAL keeps a committed event across a process crash without paying for
        # a full fsync on every event; a power loss can drop the last few.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def create_session(self, correlation_id: str, created_at: float) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO sessions(correlation_id, created_at, updated_at)"
                " VALUES (?, ?, ?)",
                (correlation_id, created_at, created_at),
            )

    def append_event(
        self,
        correlation_id: str,
        stream: str,
        event: dict[str, Any],
        updated_at: float,
    ) -> None:
        if stream not in STREAMS:
            raise ValueError(f"unknown event stream: {stream}")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO events(correlation_id, stream, body) VALUES (?, ?, ?)",
                (correlation_id, stream, json.dumps(event, ensure_ascii=False)),
            )
            self._connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE correlation_id = ?",
                (updated_at, correlation_id),
            )

    def delete_session(self, correlation_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM sessions WHERE correlation_id = ?", (correlation_id,)
            )

    def load_sessions(self) -> dict[str, dict[str, Any]]:
        """Return every persisted session in the in-memory shape ingestion uses."""
        with self._lock:
            sessions: dict[str, dict[str, Any]] = {
                correlation_id: {
                    "correlation_id": correlation_id,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "sdk_events": [],
                    "gateway_events": [],
                    "extension_events": [],
                    "is_sealed": False,
                }
                for correlation_id, created_at, updated_at in self._connection.execute(
                    "SELECT correlation_id, created_at, updated_at FROM sessions"
                    " ORDER BY created_at"
                )
            }
            for correlation_id, stream, body in self._connection.execute(
                "SELECT correlation_id, stream, body FROM events ORDER BY id"
            ):
                session = sessions.get(correlation_id)
                if session is not None and stream in STREAMS:
                    session[stream].append(json.loads(body))
        return sessions

    def close(self) -> None:
        with self._lock:
            self._connection.close()
