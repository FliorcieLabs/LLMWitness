"""Safe Effect execution, verification, idempotency, and compensation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from llmwitness.authority import AuthorityResult
from llmwitness.contracts import AgentContract, ContractCheck, ContractEngine
from llmwitness.envelope import (
    Actor,
    Authority,
    AuthorityDecision,
    Compensation,
    ContractReference,
    EffectDescriptor,
    EffectStatus,
    EvidenceReference,
    ExecutionEnvelope,
    Intent,
    Observation,
    RiskTier,
    StateReference,
    sha256_ref,
)
from llmwitness.journal import JournalConflict, WritableEffectJournal
from llmwitness.utils import canonical_json, generate_uuidv7


@dataclass(frozen=True)
class VerificationResult:
    status: EffectStatus
    evidence: tuple[EvidenceReference, ...] = ()
    external_refs: tuple[str, ...] = ()
    verifier: str | None = None
    detail: str | None = None
    observed: Any = None

    def __post_init__(self) -> None:
        if self.status not in {
            EffectStatus.VERIFIED,
            EffectStatus.FAILED,
            EffectStatus.UNKNOWN,
            EffectStatus.COMPENSATED,
        }:
            raise ValueError(
                "verification result must be verified, failed, unknown, or compensated"
            )
        if self.status in {EffectStatus.VERIFIED, EffectStatus.COMPENSATED} and (
            not self.evidence or not self.verifier
        ):
            raise ValueError(
                f"{self.status.value.upper()} requires evidence and verifier identity"
            )


@dataclass
class EffectExecution:
    effect_id: str
    run_id: str
    transaction_id: str | None
    adapter: str
    idempotency_key: str
    journal_key: str
    parameters_hash: str
    state: EffectStatus
    result: Any = None
    external_refs: tuple[str, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    verifier: str | None = None
    deduplicated: bool = False


@dataclass(frozen=True)
class EffectIdentity:
    adapter: str
    provider_key: str
    run_id: str
    trace_id: str
    agent_id: str
    principal_id: str
    policy_id: str
    authority_decision: AuthorityDecision
    approval_id: str | None
    contract_id: str
    contract_hash: str
    values_hash: str
    risk_tier: RiskTier
    transaction_id: str | None
    pre_state_ref: str
    post_state_ref: str

    @property
    def journal_key(self) -> str:
        return sha256_ref(
            canonical_json({"adapter": self.adapter, "provider_key": self.provider_key})
        )

    @property
    def scope_hash(self) -> str:
        return sha256_ref(
            canonical_json(
                {
                    "run_id": self.run_id,
                    "trace_id": self.trace_id,
                    "agent_id": self.agent_id,
                    "principal_id": self.principal_id,
                    "policy_id": self.policy_id,
                    "authority_decision": self.authority_decision.value,
                    "approval_id": self.approval_id,
                    "contract_id": self.contract_id,
                    "contract_hash": self.contract_hash,
                    "values_hash": self.values_hash,
                    "risk_tier": self.risk_tier.value,
                    "transaction_id": self.transaction_id,
                    "pre_state_ref": self.pre_state_ref,
                    "post_state_ref": self.post_state_ref,
                }
            )
        )


class EffectAdapter(Protocol):
    name: str

    async def execute(self, request: Mapping[str, Any], ctx: EffectContext) -> Any: ...

    async def verify(
        self, execution: EffectExecution, ctx: EffectContext
    ) -> VerificationResult: ...

    async def compensate(
        self, execution: EffectExecution, ctx: EffectContext
    ) -> Any: ...

    async def verify_compensation(
        self, execution: EffectExecution, ctx: EffectContext
    ) -> VerificationResult: ...

    def idempotency_key(
        self, request: Mapping[str, Any], ctx: EffectContext
    ) -> str: ...


class EffectRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, EffectAdapter] = {}

    def register(self, adapter: EffectAdapter) -> None:
        if not adapter.name or adapter.name in self._adapters:
            raise ValueError(
                f"effect adapter {adapter.name!r} is empty or already registered"
            )
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> EffectAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise KeyError(f"unknown effect adapter {name!r}") from exc


class IdempotencyCoordinator:
    def __init__(self, journal: WritableEffectJournal):
        self.journal = journal

    def prepare(
        self,
        identity: EffectIdentity,
        envelope: ExecutionEnvelope,
        *,
        parameters_hash: str,
        effect_id: str,
        execution: dict[str, Any],
        transaction_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically persist PREPARED and reserve a scoped effect identity."""
        return self.journal.prepare_effect(
            envelope,
            key=identity.journal_key,
            parameters_hash=parameters_hash,
            scope_hash=identity.scope_hash,
            effect_id=effect_id,
            run_id=identity.run_id,
            execution=execution,
            transaction_id=transaction_id,
        ).record

    def read(self, identity: EffectIdentity) -> dict[str, Any] | None:
        return self.journal.get_idempotency(identity.journal_key)


class VerificationEngine:
    def __init__(self, max_evidence_age_seconds: float | None = 300.0):
        if max_evidence_age_seconds is not None and max_evidence_age_seconds < 0:
            raise ValueError("evidence age limit must be non-negative")
        self.max_evidence_age_seconds = max_evidence_age_seconds

    def validate_freshness(self, result: VerificationResult) -> VerificationResult:
        if (
            result.status not in {EffectStatus.VERIFIED, EffectStatus.COMPENSATED}
            or self.max_evidence_age_seconds is None
        ):
            return result
        now = datetime.now(timezone.utc)
        stale = any(
            item.observed_at is not None
            and (now - item.observed_at).total_seconds() > self.max_evidence_age_seconds
            for item in result.evidence
        )
        if not stale:
            return result
        return VerificationResult(
            EffectStatus.UNKNOWN,
            evidence=result.evidence,
            external_refs=result.external_refs,
            verifier=result.verifier,
            detail="verification evidence exceeded the configured freshness window",
        )

    async def verify(
        self, adapter: EffectAdapter, execution: EffectExecution, context: EffectContext
    ) -> VerificationResult:
        return self.validate_freshness(await adapter.verify(execution, context))


@dataclass(frozen=True)
class EffectContext:
    run_id: str
    trace_id: str
    actor: Actor
    authority: AuthorityResult
    contract: AgentContract
    values: Mapping[str, Any] = field(default_factory=dict)
    transaction_id: str | None = None
    pre_state_ref: str = field(default_factory=lambda: sha256_ref(b""))
    post_state_ref: str = field(default_factory=lambda: sha256_ref(b""))
    authority_evidence: tuple[EvidenceReference, ...] = ()


class EffectRejected(RuntimeError):
    pass


class DefiniteEffectFailure(RuntimeError):
    """Adapter-confirmed failure where the external mutation did not happen."""


class SafeEffectRunner:
    def __init__(
        self,
        journal: WritableEffectJournal,
        contract_engine: ContractEngine | None = None,
        verification_engine: VerificationEngine | None = None,
        failure_hook: Callable[[str], None] | None = None,
    ):
        self.journal = journal
        self.contract_engine = contract_engine or ContractEngine()
        self.verification_engine = verification_engine or VerificationEngine()
        self.failure_hook = failure_hook

    def _envelope(
        self,
        execution: EffectExecution,
        context: EffectContext,
        status: EffectStatus,
        *,
        evidence: Sequence[EvidenceReference] = (),
        external_refs: Sequence[str] = (),
        verifier: str | None = None,
        detail: str | None = None,
    ) -> ExecutionEnvelope:
        return ExecutionEnvelope.create(
            run_id=context.run_id,
            parent_event_id=(
                None if status == EffectStatus.PLANNED else execution.effect_id
            ),
            actor=context.actor,
            intent=Intent(name=execution.adapter),
            authority=Authority(
                policy_id=context.authority.policy_id,
                decision=context.authority.decision,
                risk_tier=context.authority.risk_tier,
                approval_id=context.authority.approval_id,
            ),
            contract=ContractReference(
                contract_id=context.contract.contract_id,
                preconditions=[item.name for item in context.contract.preconditions],
                invariants=[item.name for item in context.contract.invariants],
                postconditions=[item.name for item in context.contract.postconditions],
                budgets=dict(context.contract.budgets),
            ),
            pre_state=StateReference(snapshot_ref=context.pre_state_ref),
            effect=EffectDescriptor(
                adapter=execution.adapter,
                idempotency_key=execution.idempotency_key,
                parameters_hash=execution.parameters_hash,
            ),
            observation=Observation(
                status=status,
                external_refs=list(external_refs),
                verifier=verifier,
                detail=detail,
            ),
            post_state=StateReference(snapshot_ref=context.post_state_ref),
            compensation=Compensation(
                available=True, adapter=f"{execution.adapter}.compensate"
            ),
            evidence=[*context.authority_evidence, *evidence],
            event_id=execution.effect_id if status == EffectStatus.PLANNED else None,
        )

    def _record(
        self,
        execution: EffectExecution,
        context: EffectContext,
        status: EffectStatus,
        **kwargs: Any,
    ) -> None:
        self.journal.append(
            self._envelope(execution, context, status, **kwargs),
            f"effect.{status.value}",
            transaction_id=context.transaction_id,
        )
        execution.state = status

    @staticmethod
    def _from_record(
        record: Mapping[str, Any], *, deduplicated: bool = True
    ) -> EffectExecution:
        stored = record.get("execution") or {}
        return EffectExecution(
            effect_id=str(record["effect_id"]),
            run_id=str(record["run_id"]),
            transaction_id=stored.get("transaction_id"),
            adapter=str(stored.get("adapter", "unknown")),
            idempotency_key=str(
                stored.get("provider_idempotency_key", record["idempotency_key"])
            ),
            journal_key=str(record["idempotency_key"]),
            parameters_hash=str(record["parameters_hash"]),
            state=EffectStatus(record["state"]),
            external_refs=tuple(stored.get("external_refs", [])),
            evidence=tuple(
                EvidenceReference.model_validate(item)
                for item in stored.get("evidence", [])
            ),
            verifier=stored.get("verifier"),
            deduplicated=deduplicated,
        )

    @staticmethod
    def _stored(execution: EffectExecution) -> dict[str, Any]:
        return {
            "transaction_id": execution.transaction_id,
            "adapter": execution.adapter,
            "provider_idempotency_key": execution.idempotency_key,
            "external_refs": list(execution.external_refs),
            "evidence": [item.model_dump(mode="json") for item in execution.evidence],
            "verifier": execution.verifier,
        }

    @staticmethod
    def _contract_hash(contract: AgentContract) -> str:
        def checks(values: Sequence[ContractCheck]) -> list[dict[str, Any]]:
            return [
                {
                    "name": item.name,
                    "evaluation_class": item.evaluation_class.value,
                    "blocking": item.blocking,
                }
                for item in values
            ]

        return sha256_ref(
            canonical_json(
                {
                    "contract_id": contract.contract_id,
                    "preconditions": checks(contract.preconditions),
                    "invariants": checks(contract.invariants),
                    "postconditions": checks(contract.postconditions),
                    "budgets": dict(contract.budgets),
                }
            )
        )

    async def _enforce_post_execution_contract(
        self,
        verification: VerificationResult,
        execution: EffectExecution,
        context: EffectContext,
    ) -> VerificationResult:
        if verification.status != EffectStatus.VERIFIED:
            return verification
        post_values = {
            **context.values,
            "result": (
                execution.result
                if execution.result is not None
                else verification.observed
            ),
            "external_refs": verification.external_refs,
        }
        invariant_check = await self.contract_engine.evaluate(
            context.contract,
            post_values,
            context.authority.risk_tier,
            phase="invariants",
        )
        postcondition_check = await self.contract_engine.evaluate(
            context.contract,
            post_values,
            context.authority.risk_tier,
            phase="postconditions",
        )
        if invariant_check.allowed and postcondition_check.allowed:
            return verification
        return VerificationResult(
            EffectStatus.UNKNOWN,
            evidence=verification.evidence,
            external_refs=verification.external_refs,
            verifier=verification.verifier,
            detail="post-execution contract verification failed closed",
            observed=verification.observed,
        )

    async def _resume_existing(
        self,
        record: Mapping[str, Any],
        *,
        identity: EffectIdentity,
        parameters_hash: str,
        adapter: EffectAdapter,
        context: EffectContext,
    ) -> EffectExecution:
        if (
            record["parameters_hash"] != parameters_hash
            or record["scope_hash"] != identity.scope_hash
        ):
            raise JournalConflict(
                "idempotency key was reused with different parameters or execution scope"
            )
        prior = self._from_record(record)
        if prior.adapter != adapter.name:
            raise EffectRejected("stored effect adapter does not match retry adapter")
        if prior.state != EffectStatus.UNKNOWN:
            return prior
        try:
            reconciled = await self.verification_engine.verify(adapter, prior, context)
        except Exception as exc:
            reconciled = VerificationResult(
                EffectStatus.UNKNOWN,
                detail=f"verifier exception: {type(exc).__name__}",
            )
        reconciled = await self._enforce_post_execution_contract(
            reconciled, prior, context
        )
        if reconciled.status in {EffectStatus.VERIFIED, EffectStatus.FAILED}:
            prior.evidence = reconciled.evidence
            prior.external_refs = reconciled.external_refs
            prior.verifier = reconciled.verifier
            self._record(
                prior,
                context,
                reconciled.status,
                evidence=reconciled.evidence,
                external_refs=reconciled.external_refs,
                verifier=reconciled.verifier,
                detail=reconciled.detail,
            )
            self.journal.update_idempotency(
                prior.journal_key, prior.state.value, self._stored(prior)
            )
        return prior

    async def run(
        self, adapter: EffectAdapter, request: Mapping[str, Any], context: EffectContext
    ) -> EffectExecution:
        # Hash the original canonical request so secret changes cannot alias after
        # scrubbing. Only the digest is persisted; raw request data is not journaled.
        parameters_hash = sha256_ref(canonical_json(dict(request)))
        key = adapter.idempotency_key(request, context)
        if not isinstance(adapter.name, str) or not adapter.name.strip():
            raise EffectRejected("effect adapter name must not be blank")
        if not isinstance(key, str) or not key.strip() or len(key) > 1024:
            raise EffectRejected(
                "provider idempotency key must be nonblank and bounded"
            )
        identity = EffectIdentity(
            adapter=adapter.name,
            provider_key=key,
            run_id=context.run_id,
            trace_id=context.trace_id,
            agent_id=context.actor.agent_id,
            principal_id=context.actor.principal_id,
            policy_id=context.authority.policy_id,
            authority_decision=context.authority.decision,
            approval_id=context.authority.approval_id,
            contract_id=context.contract.contract_id,
            contract_hash=self._contract_hash(context.contract),
            values_hash=sha256_ref(canonical_json(dict(context.values))),
            risk_tier=context.authority.risk_tier,
            transaction_id=context.transaction_id,
            pre_state_ref=context.pre_state_ref,
            post_state_ref=context.post_state_ref,
        )
        execution = EffectExecution(
            effect_id=generate_uuidv7(),
            run_id=context.run_id,
            transaction_id=context.transaction_id,
            adapter=adapter.name,
            idempotency_key=key,
            journal_key=identity.journal_key,
            parameters_hash=parameters_hash,
            state=EffectStatus.PLANNED,
        )
        if context.authority.decision != AuthorityDecision.ALLOW:
            self._record(execution, context, EffectStatus.PLANNED)
            raise EffectRejected(
                f"authority decision is {context.authority.decision.value}: {context.authority.reason}"
            )
        contract = await self.contract_engine.evaluate(
            context.contract, context.values, context.authority.risk_tier
        )
        if not contract.allowed:
            self._record(execution, context, EffectStatus.PLANNED)
            self._record(execution, context, EffectStatus.AUTHORIZED)
            self._record(
                execution,
                context,
                EffectStatus.FAILED,
                detail="contract precondition rejected the effect",
            )
            raise EffectRejected("contract preconditions did not authorize execution")

        invariants = await self.contract_engine.evaluate(
            context.contract,
            context.values,
            context.authority.risk_tier,
            phase="invariants",
        )
        if not invariants.allowed:
            self._record(execution, context, EffectStatus.PLANNED)
            self._record(execution, context, EffectStatus.AUTHORIZED)
            self._record(
                execution,
                context,
                EffectStatus.FAILED,
                detail="contract invariant rejected the effect",
            )
            raise EffectRejected("contract invariants did not authorize execution")

        existing = self.journal.get_idempotency(identity.journal_key)
        if existing is not None:
            try:
                return await self._resume_existing(
                    existing,
                    identity=identity,
                    parameters_hash=parameters_hash,
                    adapter=adapter,
                    context=context,
                )
            except JournalConflict:
                self._record(execution, context, EffectStatus.PLANNED)
                self._record(execution, context, EffectStatus.AUTHORIZED)
                self._record(
                    execution,
                    context,
                    EffectStatus.FAILED,
                    detail="idempotency parameters or execution scope conflicted",
                )
                raise

        self._record(execution, context, EffectStatus.PLANNED)
        self._record(execution, context, EffectStatus.AUTHORIZED)
        preparation = self.journal.prepare_effect(
            self._envelope(execution, context, EffectStatus.PREPARED),
            key=identity.journal_key,
            parameters_hash=parameters_hash,
            scope_hash=identity.scope_hash,
            effect_id=execution.effect_id,
            run_id=context.run_id,
            execution=self._stored(execution),
            transaction_id=context.transaction_id,
        )
        existing = preparation.record
        if existing["effect_id"] != execution.effect_id:
            return await self._resume_existing(
                existing,
                identity=identity,
                parameters_hash=parameters_hash,
                adapter=adapter,
                context=context,
            )

        execution.state = EffectStatus.PREPARED
        if self.failure_hook is not None:
            self.failure_hook("after_prepare_before_execute")
        self._record(execution, context, EffectStatus.EXECUTING)
        try:
            execution.result = await adapter.execute(request, context)
        except DefiniteEffectFailure as exc:
            self._record(
                execution, context, EffectStatus.FAILED, detail=type(exc).__name__
            )
            self.journal.update_idempotency(
                execution.journal_key, execution.state.value, self._stored(execution)
            )
            return execution
        except Exception as exc:
            self._record(
                execution, context, EffectStatus.UNKNOWN, detail=type(exc).__name__
            )
            self.journal.update_idempotency(
                execution.journal_key, execution.state.value, self._stored(execution)
            )
            return execution

        self._record(execution, context, EffectStatus.SUCCEEDED_UNVERIFIED)
        try:
            verification = await self.verification_engine.verify(
                adapter, execution, context
            )
        except Exception as exc:
            verification = VerificationResult(
                EffectStatus.UNKNOWN,
                detail=f"verifier exception: {type(exc).__name__}",
            )
        verification = await self._enforce_post_execution_contract(
            verification, execution, context
        )
        execution.evidence = verification.evidence
        execution.external_refs = verification.external_refs
        execution.verifier = verification.verifier
        self._record(
            execution,
            context,
            verification.status,
            evidence=verification.evidence,
            external_refs=verification.external_refs,
            verifier=verification.verifier,
            detail=verification.detail,
        )
        self.journal.update_idempotency(
            execution.journal_key, execution.state.value, self._stored(execution)
        )
        return execution

    async def compensate(
        self, adapter: EffectAdapter, execution: EffectExecution, context: EffectContext
    ) -> EffectExecution:
        if execution.state != EffectStatus.VERIFIED:
            raise EffectRejected(
                "only a verified effect can enter automatic compensation"
            )
        self._record(execution, context, EffectStatus.COMPENSATING)
        try:
            await adapter.compensate(execution, context)
            verification = self.verification_engine.validate_freshness(
                await adapter.verify_compensation(execution, context)
            )
        except Exception as exc:
            self._record(
                execution,
                context,
                EffectStatus.MANUAL_REVIEW,
                detail=type(exc).__name__,
            )
            self.journal.update_idempotency(
                execution.journal_key,
                execution.state.value,
                self._stored(execution),
            )
            return execution
        if verification.status == EffectStatus.COMPENSATED:
            self._record(
                execution,
                context,
                EffectStatus.COMPENSATED,
                evidence=verification.evidence,
                external_refs=verification.external_refs,
                verifier=verification.verifier,
            )
        else:
            self._record(
                execution,
                context,
                EffectStatus.MANUAL_REVIEW,
                detail=verification.detail,
            )
        self.journal.update_idempotency(
            execution.journal_key, execution.state.value, self._stored(execution)
        )
        return execution


__all__ = [
    "DefiniteEffectFailure",
    "EffectAdapter",
    "EffectContext",
    "EffectExecution",
    "EffectIdentity",
    "EffectRegistry",
    "EffectRejected",
    "IdempotencyCoordinator",
    "SafeEffectRunner",
    "VerificationEngine",
    "VerificationResult",
]
