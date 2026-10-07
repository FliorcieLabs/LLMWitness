"""Deterministic in-memory reference effects for demos and conformance tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from llmwitness.effects import (
    DefiniteEffectFailure,
    EffectContext,
    EffectExecution,
    VerificationResult,
)
from llmwitness.envelope import EffectStatus, EvidenceReference, sha256_ref
from llmwitness.reliability import ChaosEngine, FaultPoint
from llmwitness.utils import canonical_json


def _proof(kind: str, identifier: str, value: Any, verifier: str) -> VerificationResult:
    evidence = EvidenceReference(
        kind=kind,
        sha256=sha256_ref(canonical_json(value)),
        uri=f"memory://{identifier}",
        observed_at=datetime.now(timezone.utc),
    )
    return VerificationResult(
        EffectStatus.VERIFIED,
        evidence=(evidence,),
        external_refs=(identifier,),
        verifier=verifier,
        observed=value,
    )


def _compensation_proof(
    kind: str, identifier: str, value: Any, verifier: str
) -> VerificationResult:
    evidence = EvidenceReference(
        kind=kind,
        sha256=sha256_ref(canonical_json(value)),
        uri=f"memory://{identifier}",
        observed_at=datetime.now(timezone.utc),
    )
    return VerificationResult(
        EffectStatus.COMPENSATED,
        evidence=(evidence,),
        external_refs=(identifier,),
        verifier=verifier,
    )


class InMemoryRefundEffect:
    """Local refund simulator that can lose a response after mutating its ledger."""

    name = "reference.refund"

    def __init__(
        self, ledger: dict[str, int] | None = None, chaos: ChaosEngine | None = None
    ):
        self.ledger = ledger if ledger is not None else {}
        self._payment_by_key: dict[str, str] = {}
        self._amount_by_key: dict[str, int] = {}
        self.chaos = chaos or ChaosEngine()
        self.execute_count = 0

    def idempotency_key(self, request: Mapping[str, Any], ctx: EffectContext) -> str:
        return str(request.get("idempotency_key") or f"{request['payment_id']}:refund")

    async def execute(
        self, request: Mapping[str, Any], ctx: EffectContext
    ) -> dict[str, Any]:
        payment_id = str(request["payment_id"])
        amount = int(request["amount"])
        if amount <= 0:
            raise DefiniteEffectFailure("refund amount must be positive")
        existing = self.ledger.get(payment_id)
        if existing is not None and existing != amount:
            raise DefiniteEffectFailure("payment already has a different refund amount")
        provider_key = self.idempotency_key(request, ctx)
        self._payment_by_key[provider_key] = payment_id
        self._amount_by_key[provider_key] = amount
        if existing is None:
            self.ledger[payment_id] = amount
            self.execute_count += 1
        self.chaos.at(FaultPoint.RESPONSE_LOSS)
        return {"payment_id": payment_id, "amount": amount}

    async def verify(
        self, execution: EffectExecution, ctx: EffectContext
    ) -> VerificationResult:
        payment_id = self._payment_by_key.get(execution.idempotency_key)
        expected_amount = self._amount_by_key.get(execution.idempotency_key)
        if payment_id is None or expected_amount is None:
            return VerificationResult(
                EffectStatus.UNKNOWN,
                verifier=self.name,
                detail="authoritative simulator request state is unavailable",
            )
        amount = self.ledger.get(payment_id)
        if amount is None:
            return VerificationResult(
                EffectStatus.FAILED, verifier=self.name, detail="refund absent"
            )
        if amount != expected_amount:
            return VerificationResult(
                EffectStatus.FAILED,
                verifier=self.name,
                detail="refund amount does not match the requested amount",
            )
        return _proof(
            "authoritative_local_ledger",
            payment_id,
            {"payment_id": payment_id, "amount": amount},
            self.name,
        )

    async def compensate(self, execution: EffectExecution, ctx: EffectContext) -> None:
        payment_id = self._payment_by_key.get(
            execution.idempotency_key, execution.idempotency_key.removesuffix(":refund")
        )
        self.ledger.pop(payment_id, None)

    async def verify_compensation(
        self, execution: EffectExecution, ctx: EffectContext
    ) -> VerificationResult:
        payment_id = self._payment_by_key.get(
            execution.idempotency_key, execution.idempotency_key.removesuffix(":refund")
        )
        if payment_id in self.ledger:
            return VerificationResult(
                EffectStatus.UNKNOWN,
                verifier=self.name,
                detail="refund reversal not confirmed",
            )
        return _compensation_proof(
            "authoritative_local_ledger", payment_id, {"refunded": False}, self.name
        )


class InMemoryCRMEffect:
    """Local CRM update simulator with reversible before-state capture."""

    name = "reference.crm-update"

    def __init__(
        self,
        records: dict[str, dict[str, Any]] | None = None,
        chaos: ChaosEngine | None = None,
    ):
        self.records = records if records is not None else {}
        self.before: dict[str, dict[str, Any]] = {}
        self._record_by_key: dict[str, str] = {}
        self._fields_by_key: dict[str, dict[str, Any]] = {}
        self.chaos = chaos or ChaosEngine()
        self.execute_count = 0

    def idempotency_key(self, request: Mapping[str, Any], ctx: EffectContext) -> str:
        return str(
            request.get("idempotency_key") or f"{request['record_id']}:crm-update"
        )

    async def execute(
        self, request: Mapping[str, Any], ctx: EffectContext
    ) -> dict[str, Any]:
        record_id = str(request["record_id"])
        provider_key = self.idempotency_key(request, ctx)
        fields = dict(request.get("fields", {}))
        self._record_by_key[provider_key] = record_id
        self._fields_by_key[provider_key] = fields
        current = dict(self.records.get(record_id, {}))
        self.before.setdefault(record_id, current)
        updated = {**current, **fields}
        self.records[record_id] = updated
        self.execute_count += 1
        self.chaos.at(FaultPoint.CRASH_AFTER_EXECUTE)
        return {"record_id": record_id, "fields": updated}

    async def verify(
        self, execution: EffectExecution, ctx: EffectContext
    ) -> VerificationResult:
        record_id = self._record_by_key.get(execution.idempotency_key)
        expected_fields = self._fields_by_key.get(execution.idempotency_key)
        if record_id is None or expected_fields is None:
            return VerificationResult(
                EffectStatus.UNKNOWN,
                verifier=self.name,
                detail="authoritative simulator request state is unavailable",
            )
        value = self.records.get(record_id)
        if value is None:
            return VerificationResult(
                EffectStatus.FAILED, verifier=self.name, detail="record absent"
            )
        if any(value.get(key) != expected for key, expected in expected_fields.items()):
            return VerificationResult(
                EffectStatus.FAILED,
                verifier=self.name,
                detail="CRM state does not contain the requested field values",
            )
        return _proof(
            "authoritative_local_crm",
            record_id,
            {"record_id": record_id, "fields": value},
            self.name,
        )

    async def compensate(self, execution: EffectExecution, ctx: EffectContext) -> None:
        record_id = self._record_by_key.get(
            execution.idempotency_key,
            execution.idempotency_key.removesuffix(":crm-update"),
        )
        prior = self.before.get(record_id)
        if prior is None:
            self.records.pop(record_id, None)
        else:
            self.records[record_id] = dict(prior)

    async def verify_compensation(
        self, execution: EffectExecution, ctx: EffectContext
    ) -> VerificationResult:
        record_id = self._record_by_key.get(
            execution.idempotency_key,
            execution.idempotency_key.removesuffix(":crm-update"),
        )
        value = self.records.get(record_id, {})
        if value != self.before.get(record_id, {}):
            return VerificationResult(
                EffectStatus.UNKNOWN,
                verifier=self.name,
                detail="CRM restoration not confirmed",
            )
        return _compensation_proof(
            "authoritative_local_crm", record_id, value, self.name
        )
