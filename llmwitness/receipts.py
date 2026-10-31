"""Local signed-journal-head receipt types and issuing service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from llmwitness.utils import Ed25519KeyManager


@dataclass(frozen=True)
class SignedJournalHead:
    run_id: str
    run_sequence: int
    head_hash: str
    key_id: str
    public_key_fingerprint: str
    signed_at: str
    signature: str


class JournalHeadSigner(Protocol):
    """Minimum journal capability required to issue a signed head receipt."""

    def sign_head(
        self, run_id: str, manager: Ed25519KeyManager, key_id: str
    ) -> SignedJournalHead: ...


class ReceiptService:
    """Sign a journal head while retaining explicit key identity metadata."""

    def __init__(self, manager: Ed25519KeyManager, key_id: str):
        self.manager = manager
        self.key_id = key_id

    def issue(self, journal: JournalHeadSigner, run_id: str) -> SignedJournalHead:
        return journal.sign_head(run_id, self.manager, self.key_id)


__all__ = ["JournalHeadSigner", "ReceiptService", "SignedJournalHead"]
