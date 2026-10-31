"""Read-only recovery inbox derived from verified local journal chains."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from llmwitness.envelope import EffectStatus
from llmwitness.journal import (
    JournalConflict,
    JournalEntry,
    ReadOnlyJournal,
    SQLiteJournalStore,
)


class RecoveryAction(str, Enum):
    RECONCILE = "reconcile"
    INSPECT_OWNER = "inspect_owner"
    MANUAL_REVIEW = "manual_review"
    NONE = "none"


class RecoveryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["0.1"] = "0.1"
    run_id: str
    effect_id: str
    transaction_id: str | None = None
    adapter: str
    status: EffectStatus
    action: RecoveryAction
    reason: str


_ACTION_BY_STATUS: dict[EffectStatus, tuple[RecoveryAction, str]] = {
    EffectStatus.UNKNOWN: (
        RecoveryAction.RECONCILE,
        "authoritative reconciliation is required; blind retry is disabled",
    ),
    EffectStatus.EXECUTING: (
        RecoveryAction.RECONCILE,
        "execution may have crossed the external boundary",
    ),
    EffectStatus.SUCCEEDED_UNVERIFIED: (
        RecoveryAction.RECONCILE,
        "the reported success still requires authoritative evidence",
    ),
    EffectStatus.PREPARED: (
        RecoveryAction.INSPECT_OWNER,
        "inspect the owning run; do not execute the reserved effect blindly",
    ),
    EffectStatus.COMPENSATING: (
        RecoveryAction.MANUAL_REVIEW,
        "compensation completion has not been verified",
    ),
    EffectStatus.MANUAL_REVIEW: (
        RecoveryAction.MANUAL_REVIEW,
        "the effect already requires operator review",
    ),
}


def _effect_id(entry: JournalEntry) -> str | None:
    status = EffectStatus(entry.envelope["observation"]["status"])
    if status == EffectStatus.PLANNED:
        return str(entry.envelope["event_id"])
    parent = entry.envelope.get("parent_event_id")
    return str(parent) if parent else None


class RecoveryPlanner:
    """Build a recovery inbox without mutating the journal or invoking adapters."""

    def __init__(self, journal: ReadOnlyJournal):
        if not journal.read_only:
            raise ValueError("recovery planning requires a read-only journal")
        self.journal = journal

    def list_items(self, *, include_terminal: bool = False) -> tuple[RecoveryItem, ...]:
        items: list[RecoveryItem] = []
        for run_id in self.journal.run_ids():
            verification = self.journal.verify(run_id)
            if not verification.valid:
                raise JournalConflict(
                    f"cannot plan recovery for invalid run {run_id}: "
                    f"{verification.error}"
                )
            latest: dict[str, JournalEntry] = {}
            for entry in self.journal.scan(run_id):
                effect_id = _effect_id(entry)
                if effect_id is not None:
                    latest[effect_id] = entry
            for effect_id, entry in latest.items():
                status = EffectStatus(entry.envelope["observation"]["status"])
                action_reason = _ACTION_BY_STATUS.get(status)
                if action_reason is None and not include_terminal:
                    continue
                action, reason = action_reason or (
                    RecoveryAction.NONE,
                    "no automatic recovery action is planned for this state",
                )
                items.append(
                    RecoveryItem(
                        run_id=run_id,
                        effect_id=effect_id,
                        transaction_id=entry.transaction_id,
                        adapter=str(entry.envelope["effect"]["adapter"]),
                        status=status,
                        action=action,
                        reason=reason,
                    )
                )
        return tuple(sorted(items, key=lambda item: (item.run_id, item.effect_id)))


def list_recovery_items(
    journal_path: str, *, include_terminal: bool = False
) -> tuple[RecoveryItem, ...]:
    with SQLiteJournalStore(journal_path, read_only=True) as journal:
        return RecoveryPlanner(journal).list_items(include_terminal=include_terminal)
