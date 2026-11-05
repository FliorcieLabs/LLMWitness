"""Durable local hash-linked journal and idempotency storage."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from llmwitness.artifacts import FileArtifactStore as FileArtifactStore
from llmwitness.envelope import GENESIS_HASH, ExecutionEnvelope, sha256_ref
from llmwitness.receipts import ReceiptService as ReceiptService
from llmwitness.receipts import SignedJournalHead
from llmwitness.utils import Ed25519KeyManager, canonical_json, generate_uuidv7


class JournalConflict(RuntimeError):
    """A compare-and-set journal or idempotency operation lost a race."""


@dataclass(frozen=True)
class JournalEntry:
    entry_id: str
    run_id: str
    run_sequence: int
    transaction_id: str | None
    event_type: str
    envelope: dict[str, Any]
    previous_hash: str
    entry_hash: str
    created_at: str


@dataclass(frozen=True)
class JournalVerification:
    valid: bool
    entries_checked: int
    head_hash: str
    error: str | None = None


def verify_journal_entries(
    run_id: str, entries: Sequence[JournalEntry]
) -> JournalVerification:
    """Verify one ordered journal chain without depending on its storage adapter."""
    previous = GENESIS_HASH
    if not entries:
        return JournalVerification(False, 0, GENESIS_HASH, "run has no journal entries")
    for index, entry in enumerate(entries, start=1):
        if entry.run_id != run_id:
            return JournalVerification(
                False, index - 1, previous, f"run mismatch at sequence {index}"
            )
        if entry.run_sequence != index or entry.previous_hash != previous:
            return JournalVerification(
                False, index - 1, previous, f"chain mismatch at sequence {index}"
            )
        payload = canonical_json(
            {
                "entry_id": entry.entry_id,
                "run_id": entry.run_id,
                "run_sequence": entry.run_sequence,
                "transaction_id": entry.transaction_id,
                "event_type": entry.event_type,
                "envelope": entry.envelope,
                "previous_hash": entry.previous_hash,
                "created_at": entry.created_at,
            }
        )
        if sha256_ref(payload) != entry.entry_hash:
            return JournalVerification(
                False, index - 1, previous, f"hash mismatch at sequence {index}"
            )
        if entry.envelope.get("previous_journal_hash") != previous:
            return JournalVerification(
                False,
                index - 1,
                previous,
                f"envelope link mismatch at sequence {index}",
            )
        previous = entry.entry_hash
    return JournalVerification(True, len(entries), previous)


@dataclass(frozen=True)
class EffectPreparation:
    record: dict[str, Any]
    journal_entry: JournalEntry | None

    @property
    def created(self) -> bool:
        return self.journal_entry is not None


@runtime_checkable
class WritableEffectJournal(Protocol):
    """Minimum durable operations required by Safe Effect execution."""

    def append(
        self,
        envelope: ExecutionEnvelope,
        event_type: str,
        *,
        transaction_id: str | None = None,
        expected_head: str | None = None,
    ) -> JournalEntry: ...

    def prepare_effect(
        self,
        envelope: ExecutionEnvelope,
        *,
        key: str,
        parameters_hash: str,
        scope_hash: str,
        effect_id: str,
        run_id: str,
        execution: dict[str, Any],
        transaction_id: str | None = None,
    ) -> EffectPreparation: ...

    def get_idempotency(self, key: str) -> dict[str, Any] | None: ...

    def update_idempotency(
        self, key: str, state: str, execution: dict[str, Any] | None
    ) -> None: ...


@runtime_checkable
class ReadOnlyJournal(Protocol):
    """Minimum non-mutating journal view required by recovery planning."""

    @property
    def read_only(self) -> bool: ...

    def scan(
        self, run_id: str, transaction_id: str | None = None
    ) -> list[JournalEntry]: ...

    def verify(self, run_id: str) -> JournalVerification: ...

    def run_ids(self) -> tuple[str, ...]: ...


class JournalStore(WritableEffectJournal, ReadOnlyJournal, Protocol):
    """Compatibility protocol for callers that require the complete journal."""

    def head(self, run_id: str) -> tuple[int, str]: ...


class SQLiteJournalStore:
    """SQLite/WAL journal with per-run hash chains and atomic appends."""

    def __init__(self, path: str | os.PathLike[str], *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if read_only and not self.path.is_file():
            raise FileNotFoundError(self.path)
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if read_only:
            # SQLite's ordinary read-only WAL mode may still create ``-shm`` and
            # ``-wal`` sidecars. Immutable mode prevents all filesystem writes;
            # callers must inspect a closed/checkpointed local journal.
            uri = f"{self.path.resolve().as_uri()}?mode=ro&immutable=1"
            self._connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        if not read_only:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS journal_entries (
                entry_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                run_sequence INTEGER NOT NULL,
                transaction_id TEXT,
                event_type TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                entry_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, run_sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_journal_run ON journal_entries(run_id, run_sequence);
            CREATE INDEX IF NOT EXISTS idx_journal_transaction ON journal_entries(transaction_id, run_sequence);
            CREATE TABLE IF NOT EXISTS idempotency_records (
                idempotency_key TEXT PRIMARY KEY,
                parameters_hash TEXT NOT NULL,
                scope_hash TEXT NOT NULL DEFAULT '',
                effect_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                state TEXT NOT NULL,
                execution_json TEXT,
                updated_at TEXT NOT NULL
            );
            """)
        columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(idempotency_records)"
            ).fetchall()
        }
        if "scope_hash" not in columns:
            self._connection.execute(
                "ALTER TABLE idempotency_records "
                "ADD COLUMN scope_hash TEXT NOT NULL DEFAULT ''"
            )
        self._connection.commit()

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise PermissionError("journal was opened read-only")

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            entry_id=row["entry_id"],
            run_id=row["run_id"],
            run_sequence=row["run_sequence"],
            transaction_id=row["transaction_id"],
            event_type=row["event_type"],
            envelope=json.loads(row["envelope_json"]),
            previous_hash=row["previous_hash"],
            entry_hash=row["entry_hash"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_idempotency(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        execution_json = value.pop("execution_json")
        value["execution"] = json.loads(execution_json) if execution_json else None
        return value

    def _append_in_transaction(
        self,
        cursor: sqlite3.Cursor,
        envelope: ExecutionEnvelope,
        event_type: str,
        *,
        transaction_id: str | None = None,
        expected_head: str | None = None,
    ) -> JournalEntry:
        row = cursor.execute(
            "SELECT run_sequence, entry_hash FROM journal_entries "
            "WHERE run_id=? ORDER BY run_sequence DESC LIMIT 1",
            (envelope.run_id,),
        ).fetchone()
        sequence = 1 if row is None else int(row["run_sequence"]) + 1
        previous_hash = GENESIS_HASH if row is None else str(row["entry_hash"])
        if expected_head is not None and expected_head != previous_hash:
            raise JournalConflict("journal head changed before append")
        bound = envelope.model_copy(update={"previous_journal_hash": previous_hash})
        envelope_json = canonical_json(bound.model_dump(mode="json"))
        entry_id = generate_uuidv7()
        created_at = datetime.now(timezone.utc).isoformat()
        digest_payload = canonical_json(
            {
                "entry_id": entry_id,
                "run_id": envelope.run_id,
                "run_sequence": sequence,
                "transaction_id": transaction_id,
                "event_type": event_type,
                "envelope": json.loads(envelope_json),
                "previous_hash": previous_hash,
                "created_at": created_at,
            }
        )
        entry_hash = sha256_ref(digest_payload)
        cursor.execute(
            "INSERT INTO journal_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                envelope.run_id,
                sequence,
                transaction_id,
                event_type,
                envelope_json,
                previous_hash,
                entry_hash,
                created_at,
            ),
        )
        return JournalEntry(
            entry_id=entry_id,
            run_id=envelope.run_id,
            run_sequence=sequence,
            transaction_id=transaction_id,
            event_type=event_type,
            envelope=json.loads(envelope_json),
            previous_hash=previous_hash,
            entry_hash=entry_hash,
            created_at=created_at,
        )

    def append(
        self,
        envelope: ExecutionEnvelope,
        event_type: str,
        *,
        transaction_id: str | None = None,
        expected_head: str | None = None,
    ) -> JournalEntry:
        if not event_type:
            raise ValueError("event_type is required")
        self._ensure_writable()
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                entry = self._append_in_transaction(
                    cursor,
                    envelope,
                    event_type,
                    transaction_id=transaction_id,
                    expected_head=expected_head,
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return entry

    def scan(
        self, run_id: str, transaction_id: str | None = None
    ) -> list[JournalEntry]:
        sql = "SELECT * FROM journal_entries WHERE run_id=?"
        parameters: list[Any] = [run_id]
        if transaction_id is not None:
            sql += " AND transaction_id=?"
            parameters.append(transaction_id)
        sql += " ORDER BY run_sequence"
        rows = self._connection.execute(sql, parameters).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def head(self, run_id: str) -> tuple[int, str]:
        row = self._connection.execute(
            "SELECT run_sequence, entry_hash FROM journal_entries WHERE run_id=? ORDER BY run_sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return (
            (0, GENESIS_HASH)
            if row is None
            else (int(row["run_sequence"]), str(row["entry_hash"]))
        )

    def run_ids(self) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT DISTINCT run_id FROM journal_entries ORDER BY run_id"
        ).fetchall()
        return tuple(str(row["run_id"]) for row in rows)

    def verify(self, run_id: str) -> JournalVerification:
        return verify_journal_entries(run_id, self.scan(run_id))

    def sign_head(
        self, run_id: str, manager: Ed25519KeyManager, key_id: str
    ) -> SignedJournalHead:
        sequence, head_hash = self.head(run_id)
        signed_at = datetime.now(timezone.utc).isoformat()
        payload = canonical_json(
            {
                "run_id": run_id,
                "run_sequence": sequence,
                "head_hash": head_hash,
                "key_id": key_id,
                "signed_at": signed_at,
            }
        )
        return SignedJournalHead(
            run_id=run_id,
            run_sequence=sequence,
            head_hash=head_hash,
            key_id=key_id,
            public_key_fingerprint=manager.get_public_key_fingerprint(),
            signed_at=signed_at,
            signature=manager.sign(payload),
        )

    def prepare_effect(
        self,
        envelope: ExecutionEnvelope,
        *,
        key: str,
        parameters_hash: str,
        scope_hash: str,
        effect_id: str,
        run_id: str,
        execution: dict[str, Any],
        transaction_id: str | None = None,
    ) -> EffectPreparation:
        """Atomically journal PREPARED and reserve one scoped effect identity."""
        if envelope.observation.status.value != "prepared":
            raise ValueError("effect preparation requires a PREPARED envelope")
        if (
            envelope.run_id != run_id
            or envelope.event_id == effect_id
            or envelope.parent_event_id != effect_id
        ):
            # PREPARED must be a child transition, not the PLANNED effect event.
            raise ValueError("effect preparation has inconsistent correlation")
        if not key or not scope_hash:
            raise ValueError("idempotency key and scope hash are required")
        self._ensure_writable()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                row = cursor.execute(
                    "SELECT * FROM idempotency_records WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                if row is not None:
                    existing = self._row_to_idempotency(row)
                    if (
                        existing["parameters_hash"] != parameters_hash
                        or existing["scope_hash"] != scope_hash
                    ):
                        raise JournalConflict(
                            "idempotency key was reused with different parameters or execution scope"
                        )
                    self._connection.rollback()
                    return EffectPreparation(existing, None)

                entry = self._append_in_transaction(
                    cursor,
                    envelope,
                    "effect.prepared",
                    transaction_id=transaction_id,
                )
                cursor.execute(
                    """
                    INSERT INTO idempotency_records (
                        idempotency_key, parameters_hash, scope_hash, effect_id,
                        run_id, state, execution_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        parameters_hash,
                        scope_hash,
                        effect_id,
                        run_id,
                        "prepared",
                        canonical_json(execution),
                        now,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        record = {
            "idempotency_key": key,
            "parameters_hash": parameters_hash,
            "scope_hash": scope_hash,
            "effect_id": effect_id,
            "run_id": run_id,
            "state": "prepared",
            "updated_at": now,
            "execution": execution,
        }
        return EffectPreparation(record, entry)

    def reserve_idempotency(
        self,
        key: str,
        parameters_hash: str,
        effect_id: str,
        run_id: str,
        *,
        scope_hash: str = "",
        execution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compatibility reservation API; SafeEffectRunner uses prepare_effect()."""
        self._ensure_writable()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO idempotency_records (
                        idempotency_key, parameters_hash, scope_hash, effect_id,
                        run_id, state, execution_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        parameters_hash,
                        scope_hash,
                        effect_id,
                        run_id,
                        "prepared",
                        canonical_json(execution) if execution is not None else None,
                        now,
                    ),
                )
                self._connection.commit()
                return self.get_idempotency(key) or {}
            except sqlite3.IntegrityError:
                self._connection.rollback()
                existing = self.get_idempotency(key)
                if (
                    existing is None
                    or existing["parameters_hash"] != parameters_hash
                    or existing["scope_hash"] != scope_hash
                ):
                    raise JournalConflict(
                        "idempotency key was reused with different parameters or execution scope"
                    ) from None
                return existing

    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM idempotency_records WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_idempotency(row)

    def update_idempotency(
        self, key: str, state: str, execution: dict[str, Any] | None
    ) -> None:
        self._ensure_writable()
        with self._lock:
            result = self._connection.execute(
                "UPDATE idempotency_records SET state=?, execution_json=?, updated_at=? WHERE idempotency_key=?",
                (
                    state,
                    canonical_json(execution) if execution is not None else None,
                    datetime.now(timezone.utc).isoformat(),
                    key,
                ),
            )
            if result.rowcount != 1:
                raise KeyError(key)
            self._connection.commit()

    def flush(self) -> None:
        self._ensure_writable()
        self._connection.commit()
        self._connection.execute("PRAGMA wal_checkpoint(FULL)")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteJournalStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
