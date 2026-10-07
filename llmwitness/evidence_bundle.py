"""Portable, offline-verifiable bundles for one local journal run."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from llmwitness.envelope import read_envelope, sha256_ref
from llmwitness.journal import (
    JournalEntry,
    ReadOnlyJournal,
    verify_journal_entries,
)
from llmwitness.utils import canonical_json, redact_payload

BUNDLE_SCHEMA_VERSION = "0.1"
BUNDLE_TYPE = "llmwitness.local-evidence"
BUNDLE_RECORDS_NAME = "journal.json"
BUNDLE_MANIFEST_NAME = "manifest.json"
DEFAULT_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_EXPECTED_MEMBERS = (BUNDLE_RECORDS_NAME, BUNDLE_MANIFEST_NAME)
_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_LIMITATIONS = (
    "Local hash-chain verification is tamper-evident, not immutable or WORM storage.",
    "The bundle records local evidence and does not prove that an external event is true.",
    "Referenced artifact bytes are not embedded or fetched by this format version.",
)


def _require_uuidv7(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("identifier must be an RFC 9562 UUIDv7") from exc
    if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
        raise ValueError("identifier must be an RFC 9562 UUIDv7")
    return str(parsed)


class EvidenceBundleManifest(BaseModel):
    """Versioned public manifest for a local evidence bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["0.1"] = "0.1"
    bundle_type: Literal["llmwitness.local-evidence"] = "llmwitness.local-evidence"
    run_id: str
    entry_count: int = Field(ge=1)
    head_hash: str
    journal_sha256: str
    evidence_references: tuple[str, ...] = ()
    limitations: tuple[str, ...] = _LIMITATIONS

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        return _require_uuidv7(value)

    @field_validator("head_hash", "journal_sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256_REF.fullmatch(value):
            raise ValueError("value must be a sha256:<hex> reference")
        return value

    @field_validator("evidence_references")
    @classmethod
    def validate_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("evidence references must be unique and sorted")
        if any(not _SHA256_REF.fullmatch(item) for item in value):
            raise ValueError("evidence reference must be a sha256:<hex> reference")
        return value


class _JournalEntryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_id: str
    run_id: str
    run_sequence: int = Field(ge=1)
    transaction_id: str | None
    event_type: str = Field(min_length=1, max_length=256)
    envelope: dict[str, Any]
    previous_hash: str
    entry_hash: str
    created_at: str

    @field_validator("entry_id", "run_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _require_uuidv7(value)

    @field_validator("previous_hash", "entry_hash")
    @classmethod
    def validate_hashes(cls, value: str) -> str:
        if not _SHA256_REF.fullmatch(value):
            raise ValueError("journal hash must be a sha256:<hex> reference")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    def to_entry(self) -> JournalEntry:
        return JournalEntry(**self.model_dump(mode="python"))


@dataclass(frozen=True)
class EvidenceBundleVerification:
    valid: bool
    run_id: str | None
    entries_checked: int
    head_hash: str | None
    evidence_references: tuple[str, ...] = ()
    error: str | None = None


def _validate_portable_uri(uri: str | None) -> None:
    if uri is None:
        return
    posix_path = PurePosixPath(uri)
    windows_path = PureWindowsPath(uri)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or ".." in posix_path.parts
        or ".." in windows_path.parts
        or uri.startswith(("file:", "\\\\"))
    ):
        raise ValueError("evidence URI exposes or traverses a local filesystem path")
    parsed = urlsplit(uri)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "evidence URI must not contain credentials, query, or fragment"
        )


def _validated_entries(raw_entries: Any) -> tuple[JournalEntry, ...]:
    if not isinstance(raw_entries, list):
        raise ValueError("journal entries must be a JSON array")
    entries: list[JournalEntry] = []
    for raw in raw_entries:
        record = _JournalEntryRecord.model_validate(raw)
        metadata = {
            "event_type": record.event_type,
            "transaction_id": record.transaction_id,
        }
        if redact_payload(metadata) != metadata:
            raise ValueError("journal metadata contains a recognized raw secret")
        envelope = read_envelope(record.envelope)
        if envelope.run_id != record.run_id:
            raise ValueError("journal entry and envelope run identifiers differ")
        for evidence in envelope.evidence:
            _validate_portable_uri(evidence.uri)
        entries.append(record.to_entry())
    return tuple(entries)


def _evidence_references(entries: tuple[JournalEntry, ...]) -> tuple[str, ...]:
    references = {
        str(item["sha256"])
        for entry in entries
        for item in entry.envelope.get("evidence", [])
    }
    return tuple(sorted(references))


def _zip_member(name: str, content: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o600 << 16
    info.flag_bits |= 0x800
    return info, content


def export_evidence_bundle(
    journal: ReadOnlyJournal,
    run_id: str,
    destination: str | os.PathLike[str],
    *,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> EvidenceBundleManifest:
    """Atomically export one verified run without embedding source paths or blobs."""
    normalized_run_id = _require_uuidv7(run_id)
    if max_bundle_bytes <= 0:
        raise ValueError("max_bundle_bytes must be positive")
    source_verification = journal.verify(normalized_run_id)
    if not source_verification.valid:
        raise ValueError(
            f"source journal verification failed: {source_verification.error}"
        )

    entries = _validated_entries(
        [asdict(entry) for entry in journal.scan(normalized_run_id)]
    )
    chain_verification = verify_journal_entries(normalized_run_id, entries)
    if not chain_verification.valid:
        raise ValueError(
            f"source journal verification failed: {chain_verification.error}"
        )

    records_bytes = canonical_json(
        {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "entries": [asdict(e) for e in entries],
        }
    ).encode("utf-8")
    manifest = EvidenceBundleManifest(
        run_id=normalized_run_id,
        entry_count=len(entries),
        head_hash=chain_verification.head_hash,
        journal_sha256=sha256_ref(records_bytes),
        evidence_references=_evidence_references(entries),
    )
    manifest_bytes = canonical_json(manifest.model_dump(mode="json")).encode("utf-8")
    if len(records_bytes) + len(manifest_bytes) > max_bundle_bytes:
        raise ValueError("evidence bundle exceeds configured size limit")

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(*_zip_member(BUNDLE_RECORDS_NAME, records_bytes))
            archive.writestr(*_zip_member(BUNDLE_MANIFEST_NAME, manifest_bytes))
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def verify_evidence_bundle(
    bundle: str | os.PathLike[str],
    *,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> EvidenceBundleVerification:
    """Verify a bundle entirely offline without extracting archive members."""
    if max_bundle_bytes <= 0:
        return EvidenceBundleVerification(
            False, None, 0, None, error="max_bundle_bytes must be positive"
        )
    try:
        path = Path(bundle)
        if path.stat().st_size > max_bundle_bytes:
            raise ValueError("evidence bundle exceeds configured size limit")
        with zipfile.ZipFile(path, "r") as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if names != list(_EXPECTED_MEMBERS) or len(set(names)) != len(names):
                raise ValueError("evidence bundle has unexpected or duplicate members")
            if any(
                item.is_dir()
                or item.compress_type != zipfile.ZIP_STORED
                or item.file_size > max_bundle_bytes
                for item in members
            ):
                raise ValueError("evidence bundle member violates type or size limit")
            if sum(item.file_size for item in members) > max_bundle_bytes:
                raise ValueError("evidence bundle exceeds configured size limit")
            records_bytes = archive.read(BUNDLE_RECORDS_NAME)
            manifest_bytes = archive.read(BUNDLE_MANIFEST_NAME)

        manifest = EvidenceBundleManifest.model_validate_json(manifest_bytes)
        if sha256_ref(records_bytes) != manifest.journal_sha256:
            raise ValueError("journal payload digest does not match the manifest")
        records = json.loads(records_bytes)
        if set(records) != {"schema_version", "entries"}:
            raise ValueError("journal payload has unexpected fields")
        if records["schema_version"] != BUNDLE_SCHEMA_VERSION:
            raise ValueError("unsupported journal payload schema version")
        entries = _validated_entries(records["entries"])
        chain = verify_journal_entries(manifest.run_id, entries)
        if not chain.valid:
            raise ValueError(chain.error or "journal chain verification failed")
        if manifest.entry_count != len(entries):
            raise ValueError("manifest entry count does not match journal payload")
        if manifest.head_hash != chain.head_hash:
            raise ValueError("manifest head hash does not match journal payload")
        references = _evidence_references(entries)
        if manifest.evidence_references != references:
            raise ValueError(
                "manifest evidence references do not match journal payload"
            )
        return EvidenceBundleVerification(
            True,
            manifest.run_id,
            chain.entries_checked,
            chain.head_hash,
            references,
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as exc:
        return EvidenceBundleVerification(False, None, 0, None, error=str(exc))


__all__ = [
    "BUNDLE_MANIFEST_NAME",
    "BUNDLE_RECORDS_NAME",
    "BUNDLE_SCHEMA_VERSION",
    "BUNDLE_TYPE",
    "DEFAULT_MAX_BUNDLE_BYTES",
    "EvidenceBundleManifest",
    "EvidenceBundleVerification",
    "export_evidence_bundle",
    "verify_evidence_bundle",
]
