"""On-disk spool for telemetry that could not be delivered.

Delivery stays at-least-once and best-effort: an event that cannot reach the
ingestion service is appended to a local JSON-lines file and re-sent later. It
can still be lost if the spool is full, the disk fails, or the session it
belongs to has been sealed by the time it is replayed.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SPOOL_DIR = ".llmwitness/spool"
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_STALE_CLAIM_SECONDS = 60.0
_DISABLED_VALUES = {"", "off", "none", "false", "0", "disabled"}


@dataclass
class SpoolClaim:
    """Spooled events taken for one replay attempt."""

    path: Path
    payloads: list[dict[str, Any]] = field(default_factory=list)


def is_disabled(value: str) -> bool:
    """Whether a path-style setting holds one of the "turn this off" values."""
    return value.strip().lower() in _DISABLED_VALUES


def classify_delivery(status_code: int) -> str:
    """Map an ingestion HTTP status to ``delivered``, ``retry`` or ``discard``.

    A rejected event (bad payload, sealed session) will never succeed, so it
    is discarded; a busy or failing service might accept it later.
    """
    if status_code < 300:
        return "delivered"
    if status_code in {408, 425, 429} or status_code >= 500:
        return "retry"
    return "discard"


def spool_directory_from_env() -> Path | None:
    """Resolve ``LLMWITNESS_SPOOL_DIR``; ``off`` disables spooling."""
    value = os.getenv("LLMWITNESS_SPOOL_DIR")
    if value is None:
        return Path(DEFAULT_SPOOL_DIR)
    if is_disabled(value):
        return None
    return Path(value)


class EventSpool:
    """Append-only spool with claim-and-release replay, safe across processes."""

    def __init__(self, directory: str | Path, max_bytes: int = DEFAULT_MAX_BYTES):
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def _main_path(self, stream: str) -> Path:
        self._validate_stream(stream)
        return self.directory / f"{stream}.jsonl"

    @staticmethod
    def _validate_stream(stream: str) -> None:
        if (
            not isinstance(stream, str)
            or re.fullmatch(r"[A-Za-z0-9_-]+", stream) is None
        ):
            raise ValueError("stream must be a nonempty filename identifier")

    def _size(self) -> int:
        try:
            return sum(
                item.stat().st_size
                for item in self.directory.iterdir()
                if item.is_file()
            )
        except OSError:
            return 0

    def append(self, stream: str, payload: dict[str, Any]) -> bool:
        """Persist one event; returns False when it could not be kept."""
        self._validate_stream(stream)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")
        with self._lock:
            try:
                if not self.directory.is_dir():
                    # Spooled telemetry is scrubbed but still private to its owner.
                    self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self._size() + len(encoded) > self.max_bytes:
                    return False
                # One O_APPEND write per event keeps lines whole when several
                # processes share a spool directory.
                descriptor = os.open(
                    self._main_path(stream),
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                try:
                    os.write(descriptor, encoded)
                finally:
                    os.close(descriptor)
            except OSError:
                return False
        return True

    def has_pending(self, stream: str) -> bool:
        try:
            if self._main_path(stream).stat().st_size > 0:
                return True
        except OSError:
            pass
        return any(self._stale_claims(stream))

    def _stale_claims(self, stream: str) -> list[Path]:
        self._validate_stream(stream)
        cutoff = time.time() - _STALE_CLAIM_SECONDS
        try:
            return [
                item
                for item in self.directory.glob(f"{stream}.*.draining")
                if item.stat().st_mtime < cutoff
            ]
        except OSError:
            return []

    def claim(self, stream: str) -> SpoolClaim | None:
        """Atomically take one pending batch, including abandoned replay work."""
        with self._lock:
            claim_path = self.directory / f"{stream}.{secrets.token_hex(6)}.draining"
            sources = [self._main_path(stream), *self._stale_claims(stream)]
            for source in sources:
                try:
                    os.replace(source, claim_path)
                except OSError:
                    continue
                break
            else:
                return None
            try:
                # Rename preserves mtime: refresh the lease so an old batch
                # cannot immediately be adopted by another replay worker.
                os.utime(claim_path, None)
                content = claim_path.read_bytes()
            except OSError:
                # Leave the unread batch available for later lease recovery.
                return None
            claim = SpoolClaim(claim_path)
            for line in content.splitlines():
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue  # a torn or corrupt line cannot be replayed
                if isinstance(payload, dict):
                    claim.payloads.append(payload)
            return claim

    def release(
        self, stream: str, claim: SpoolClaim, remaining: list[dict[str, Any]]
    ) -> None:
        """Retain undelivered events without consuming additional spool capacity.

        Replace the owned batch atomically before relinquishing it. A failed
        write preserves the original batch for lease recovery; this can cause
        duplicate delivery, consistent with the spool's at-least-once contract.
        """
        self._validate_stream(stream)
        if (
            claim.path.parent.resolve() != self.directory.resolve()
            or re.fullmatch(
                rf"{re.escape(stream)}\.[0-9a-f]{{12}}\.draining", claim.path.name
            )
            is None
        ):
            raise ValueError("claim must belong to this spool stream")
        if remaining:
            temporary_path: Path | None = None
            try:
                descriptor, name = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
                temporary_path = Path(name)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                    for payload in remaining:
                        handle.write(
                            json.dumps(
                                payload, ensure_ascii=False, separators=(",", ":")
                            )
                            + "\n"
                        )
                    handle.flush()
                    os.fsync(handle.fileno())
                # Publish as reclaimable in the same atomic replacement.
                os.utime(temporary_path, (0, 0))
                os.replace(temporary_path, claim.path)
            except OSError:
                pass  # Preserve the original claim if replacement failed.
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            return
        try:
            claim.path.unlink()
        except OSError:
            pass
