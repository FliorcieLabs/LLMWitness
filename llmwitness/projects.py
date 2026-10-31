"""Strict, local-only project definitions and reference-effect job execution.

This module intentionally supports only the two dependency-free in-memory
reference effects.  It is a bootstrap surface for Community examples, not a
generic plugin loader or an external-effect executor.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from llmwitness.authority import AuthorityEngine, AuthorityPolicy, AuthorityResult
from llmwitness.contracts import AgentContract, ContractCheck, EvaluationClass
from llmwitness.effects import (
    EffectAdapter,
    EffectContext,
    EffectExecution,
    EffectRejected,
    SafeEffectRunner,
)
from llmwitness.envelope import Actor, EffectStatus, RiskTier, new_trace_id, sha256_ref
from llmwitness.journal import JournalConflict, SQLiteJournalStore
from llmwitness.planning import EffectPlan, EffectPlanner
from llmwitness.reference_effects import InMemoryCRMEffect, InMemoryRefundEffect
from llmwitness.reliability import ChaosEngine, FaultInjector, FaultPoint
from llmwitness.utils import canonical_json, generate_uuidv7, redact_payload

PROJECT_SCHEMA_VERSION = "0.1"
MAX_PROJECT_CONFIG_BYTES = 256 * 1024
MAX_JSON_VALUE_DEPTH = 8
MAX_JSON_VALUE_NODES = 1_000
MAX_JSON_STRING_LENGTH = 8_192
ReferenceAdapterName = Literal["reference.refund", "reference.crm-update"]
RunOutcome = Literal["completed", "recovered", "rejected", "failed", "unresolved"]


class StrictProjectModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProjectActorConfig(StrictProjectModel):
    agent_id: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(min_length=1, max_length=256)

    @field_validator("agent_id", "principal_id")
    @classmethod
    def reject_blank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("actor identifiers must not be blank")
        return value


class ProjectAuthorityConfig(StrictProjectModel):
    policy_id: str = Field(min_length=1, max_length=256)
    allowed_effects: tuple[ReferenceAdapterName, ...] = Field(
        min_length=1, max_length=2
    )
    allowed_principals: tuple[str, ...] = Field(min_length=1, max_length=100)
    maximum_risk: RiskTier = RiskTier.R2
    maximum_delegation_depth: int = Field(default=0, ge=0)

    @field_validator("policy_id")
    @classmethod
    def reject_blank_policy(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("policy_id must not be blank")
        return value

    @field_validator("allowed_effects", "allowed_principals")
    @classmethod
    def reject_duplicates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("authority allowlists must not contain blank values")
        if len(value) != len(set(value)):
            raise ValueError("authority allowlists must not contain duplicates")
        return value


class ProjectContractConfig(StrictProjectModel):
    contract_id: str = Field(min_length=1, max_length=256)
    budgets: dict[str, float] = Field(default_factory=dict)

    @field_validator("contract_id")
    @classmethod
    def reject_blank_contract(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("contract_id must not be blank")
        return value

    @field_validator("budgets")
    @classmethod
    def validate_budgets(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not name.strip() for name in value):
            raise ValueError("budget names must not be blank")
        if any(limit < 0 or not math.isfinite(limit) for limit in value.values()):
            raise ValueError("budget limits must be finite and non-negative")
        return value


class ProjectJournalConfig(StrictProjectModel):
    path: str = Field(default=".llmwitness/journal.db", min_length=1, max_length=1024)

    @field_validator("path")
    @classmethod
    def require_relative_safe_shape(cls, value: str) -> str:
        candidate = Path(value)
        if not value.strip() or candidate.is_absolute() or candidate.anchor:
            raise ValueError("journal path must be project-relative")
        if ".." in candidate.parts:
            raise ValueError("journal path must not contain '..'")
        return value


class RefundRequest(StrictProjectModel):
    payment_id: str = Field(min_length=1, max_length=256)
    amount: int = Field(gt=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("payment_id", "idempotency_key")
    @classmethod
    def reject_blank_value(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("refund identifiers must not be blank")
        return value


class CRMUpdateRequest(StrictProjectModel):
    record_id: str = Field(min_length=1, max_length=256)
    fields: dict[str, JsonValue] = Field(min_length=1, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("record_id", "idempotency_key")
    @classmethod
    def reject_blank_value(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("CRM identifiers must not be blank")
        return value

    @model_validator(mode="after")
    def limit_field_shape(self) -> CRMUpdateRequest:
        _validate_json_shape(self.fields)
        return self


class ProjectJobBase(StrictProjectModel):
    job_id: str = Field(min_length=1, max_length=128)
    risk_tier: RiskTier
    approval_id: str | None = Field(default=None, min_length=1, max_length=256)
    delegation_depth: int = Field(default=0, ge=0)
    contract: ProjectContractConfig | None = None

    @field_validator("job_id")
    @classmethod
    def reject_blank_job(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("job_id must not be blank")
        return value

    @field_validator("approval_id")
    @classmethod
    def reject_blank_approval(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("approval_id must not be blank")
        return value


class RefundJob(ProjectJobBase):
    adapter: Literal["reference.refund"]
    request: RefundRequest
    simulate_response_loss: bool = False


class CRMUpdateJob(ProjectJobBase):
    adapter: Literal["reference.crm-update"]
    request: CRMUpdateRequest


ProjectJob = Annotated[RefundJob | CRMUpdateJob, Field(discriminator="adapter")]


class ProjectConfig(StrictProjectModel):
    schema_version: Literal["0.1"] = "0.1"
    project_id: str = Field(min_length=1, max_length=128)
    actor: ProjectActorConfig
    authority: ProjectAuthorityConfig
    contract: ProjectContractConfig
    journal: ProjectJournalConfig = Field(default_factory=ProjectJournalConfig)
    jobs: tuple[ProjectJob, ...] = Field(min_length=1, max_length=100)

    @field_validator("project_id")
    @classmethod
    def reject_blank_project(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project_id must not be blank")
        return value

    @model_validator(mode="after")
    def require_unique_jobs(self) -> ProjectConfig:
        job_ids = [job.job_id for job in self.jobs]
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("project job_id values must be unique")
        for job in self.jobs:
            configured_contract = job.contract or self.contract
            budget_names = set(configured_contract.budgets)
            supported = (
                {"amount"}
                if isinstance(job, RefundJob)
                else {"record_updates", "field_updates"}
            )
            unknown = budget_names - supported
            if unknown:
                raise ValueError(
                    f"job {job.job_id!r} cannot derive budget metrics {sorted(unknown)!r}"
                )

        scan_payload = {
            "project_id": self.project_id,
            "actor": self.actor.model_dump(mode="json"),
            "authority": self.authority.model_dump(mode="json"),
            "contract": self.contract.model_dump(mode="json"),
            "jobs": [job.model_dump(mode="json") for job in self.jobs],
        }
        if redact_payload(scan_payload) != scan_payload:
            raise ValueError(
                "project config contains a value recognized by best-effort pattern scrubbing"
            )
        return self


class ProjectRunResult(StrictProjectModel):
    schema_version: Literal["0.1"] = "0.1"
    project_id: str
    job_id: str
    adapter: ReferenceAdapterName
    run_id: str
    attempt_run_id: str | None = None
    effect_id: str | None = None
    effect_status: EffectStatus | None = None
    outcome: RunOutcome
    attempts: int = Field(ge=1)
    dispatch_count: int = Field(ge=0)
    deduplicated: bool = False
    external_refs: tuple[str, ...] = ()
    journal_path: str
    journal_valid: bool
    journal_entries: int = Field(ge=0)
    attempt_journal_entries: int = Field(ge=0)
    detail: str | None = None

    @field_validator("run_id", "attempt_run_id", "effect_id")
    @classmethod
    def require_uuidv7(cls, value: str | None) -> str | None:
        if value is None:
            return None
        import uuid

        parsed = uuid.UUID(value)
        if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
            raise ValueError("project run identifiers must be RFC 9562 UUIDv7")
        return str(parsed)


class ProjectBatchResult(StrictProjectModel):
    """Outcome of sequential independent reference jobs, not an ACID transaction."""

    schema_version: Literal["0.1"] = "0.1"
    project_id: str
    outcome: RunOutcome
    results: tuple[ProjectRunResult, ...]


@dataclass(frozen=True)
class LoadedProject:
    config_path: Path
    root: Path
    journal_path: Path
    config: ProjectConfig


def _validate_json_shape(value: JsonValue) -> None:
    nodes = 0

    def visit(item: JsonValue, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_VALUE_NODES:
            raise ValueError("CRM fields exceed the JSON node limit")
        if depth > MAX_JSON_VALUE_DEPTH:
            raise ValueError("CRM fields exceed the JSON nesting limit")
        if isinstance(item, str) and len(item) > MAX_JSON_STRING_LENGTH:
            raise ValueError("CRM field string exceeds the size limit")
        if isinstance(item, dict):
            for key, child in item.items():
                if not key.strip() or len(key) > 256:
                    raise ValueError("CRM field names must be nonblank and bounded")
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)

    visit(value, 0)


def _resolve_storage_path(project_root: Path, relative_path: str) -> Path:
    """Resolve a configured path beneath the project's local storage directory."""
    root = project_root.resolve(strict=True)
    storage_root = (root / ".llmwitness").resolve(strict=False)
    try:
        storage_root.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "project .llmwitness directory escapes the project root"
        ) from exc

    candidate = Path(relative_path)
    if candidate.is_absolute() or candidate.anchor or ".." in candidate.parts:
        raise ValueError("project paths must be relative and must not contain '..'")
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(storage_root)
    except ValueError as exc:
        raise ValueError("journal path must remain under project .llmwitness") from exc
    return resolved


def load_project(path: str | Path) -> LoadedProject:
    """Validate an entire project file before creating any project state."""
    supplied_path = Path(path)
    if supplied_path.is_symlink():
        raise ValueError("project config path must not be a symlink")
    config_path = supplied_path.resolve(strict=True)
    if not config_path.is_file():
        raise ValueError("project config path must identify a file")
    raw_bytes = config_path.read_bytes()
    if len(raw_bytes) > MAX_PROJECT_CONFIG_BYTES:
        raise ValueError("project config exceeds the 256 KiB size limit")
    raw = raw_bytes.decode("utf-8")
    config = ProjectConfig.model_validate_json(raw)
    root = config_path.parent.resolve(strict=True)
    journal_path = _resolve_storage_path(root, config.journal.path)
    return LoadedProject(config_path, root, journal_path, config)


def _starter_project(project_id: str) -> ProjectConfig:
    return ProjectConfig.model_validate_json(
        json.dumps(
            {
                "schema_version": PROJECT_SCHEMA_VERSION,
                "project_id": project_id,
                "actor": {
                    "agent_id": "reference-workflow-agent",
                    "principal_id": "local-user",
                },
                "authority": {
                    "policy_id": "reference-local-policy",
                    "allowed_effects": [
                        "reference.refund",
                        "reference.crm-update",
                    ],
                    "allowed_principals": ["local-user"],
                    "maximum_risk": "R2",
                    "maximum_delegation_depth": 0,
                },
                "contract": {
                    "contract_id": "refund-local-contract",
                    "budgets": {"amount": 100},
                },
                "journal": {"path": ".llmwitness/journal.db"},
                "jobs": [
                    {
                        "job_id": "refund-payment",
                        "adapter": "reference.refund",
                        "risk_tier": "R2",
                        "contract": {
                            "contract_id": "refund-local-contract",
                            "budgets": {"amount": 100},
                        },
                        "request": {
                            "payment_id": "payment-demo-001",
                            "amount": 25,
                        },
                        "simulate_response_loss": True,
                    },
                    {
                        "job_id": "update-customer",
                        "adapter": "reference.crm-update",
                        "risk_tier": "R2",
                        "contract": {
                            "contract_id": "crm-local-contract",
                            "budgets": {
                                "record_updates": 1,
                                "field_updates": 3,
                            },
                        },
                        "request": {
                            "record_id": "customer-demo-001",
                            "fields": {
                                "status": "refunded",
                                "workflow": "reference",
                            },
                        },
                    },
                ],
            }
        )
    )


def bootstrap_project(directory: str | Path) -> Path:
    """Create a deterministic local reference project without overwriting files."""
    requested = Path(directory)
    if requested.exists() and requested.is_symlink():
        raise ValueError("project directory must not be a symlink")
    if requested.exists():
        if not requested.is_dir():
            raise ValueError("project destination must be a directory")
        if any(requested.iterdir()):
            raise FileExistsError("project destination must be empty")
    else:
        requested.mkdir(parents=True)
    root = requested.resolve(strict=True)
    project_id = root.name.strip() or "reference-project"
    config = _starter_project(project_id)
    config_path = root / "fliorcie.project.json"
    ignore_path = root / ".gitignore"
    config_text = json.dumps(config.model_dump(mode="json"), indent=2) + "\n"
    with config_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(config_text)
    with ignore_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(".llmwitness/\n")
    return config_path


class ProjectRunner:
    """Execute validated jobs against local in-memory reference effects only."""

    def __init__(self, project: LoadedProject):
        self.project = project
        self._adapters: dict[ReferenceAdapterName, EffectAdapter] = {}

    @classmethod
    def from_file(cls, path: str | Path) -> ProjectRunner:
        return cls(load_project(path))

    def _job(self, job_id: str) -> ProjectJob:
        for job in self.project.config.jobs:
            if job.job_id == job_id:
                return job
        raise KeyError(f"unknown project job {job_id!r}")

    def _adapter(self, name: ReferenceAdapterName) -> EffectAdapter:
        adapter = self._adapters.get(name)
        if adapter is not None:
            return adapter
        if name == "reference.refund":
            adapter = InMemoryRefundEffect()
        elif name == "reference.crm-update":
            adapter = InMemoryCRMEffect()
        else:  # pragma: no cover - discriminated strict models close this branch
            raise ValueError(f"unsupported local reference adapter {name!r}")
        self._adapters[name] = adapter
        return adapter

    def _authority(self, job: ProjectJob) -> AuthorityResult:
        configured = self.project.config.authority
        policy = AuthorityPolicy(
            policy_id=configured.policy_id,
            allowed_effects=frozenset(configured.allowed_effects),
            allowed_principals=frozenset(configured.allowed_principals),
            maximum_risk=configured.maximum_risk,
            maximum_delegation_depth=configured.maximum_delegation_depth,
        )
        return AuthorityEngine().evaluate(
            policy,
            effect_name=job.adapter,
            principal_id=self.project.config.actor.principal_id,
            risk_tier=job.risk_tier,
            delegation_depth=job.delegation_depth,
            approval_id=job.approval_id,
        )

    def _contract(self, job: ProjectJob) -> AgentContract:
        configured = job.contract or self.project.config.contract
        # Parsing the discriminated request model is a deterministic precondition.
        schema_check = ContractCheck(
            "strict-project-config-v0.1",
            EvaluationClass.DETERMINISTIC,
            lambda _: True,
        )
        if isinstance(job, RefundJob):
            expected_payment = job.request.payment_id
            expected_amount = job.request.amount

            def requested_result(values: Mapping[str, Any]) -> bool:
                result = values.get("result")
                return (
                    isinstance(result, Mapping)
                    and result.get("payment_id") == expected_payment
                    and result.get("amount") == expected_amount
                )

        else:
            expected_record = job.request.record_id
            expected_fields = dict(job.request.fields)

            def requested_result(values: Mapping[str, Any]) -> bool:
                result = values.get("result")
                fields = result.get("fields") if isinstance(result, Mapping) else None
                return (
                    isinstance(result, Mapping)
                    and result.get("record_id") == expected_record
                    and isinstance(fields, Mapping)
                    and all(
                        fields.get(key) == value
                        for key, value in expected_fields.items()
                    )
                )

        postcondition = ContractCheck(
            "requested-local-state-observed",
            EvaluationClass.DETERMINISTIC,
            requested_result,
        )
        return AgentContract(
            configured.contract_id,
            preconditions=(schema_check,),
            postconditions=(postcondition,),
            budgets=dict(configured.budgets),
        )

    @staticmethod
    def _budget_values(job: ProjectJob) -> dict[str, float]:
        if isinstance(job, RefundJob):
            return {"amount": float(job.request.amount)}
        return {
            "record_updates": 1.0,
            "field_updates": float(len(job.request.fields)),
        }

    @staticmethod
    def _requested_state_exists(job: ProjectJob, adapter: EffectAdapter) -> bool:
        if isinstance(job, RefundJob):
            return (
                isinstance(adapter, InMemoryRefundEffect)
                and adapter.ledger.get(job.request.payment_id) == job.request.amount
            )
        if not isinstance(adapter, InMemoryCRMEffect):
            return False
        current = adapter.records.get(job.request.record_id)
        return current is not None and all(
            current.get(key) == value for key, value in job.request.fields.items()
        )

    def _request(self, job: ProjectJob) -> dict[str, Any]:
        request = job.request.model_dump(mode="json", exclude_none=True)
        caller_key = request.pop("idempotency_key", None)
        if caller_key is None:
            caller_key = (
                job.request.payment_id
                if isinstance(job, RefundJob)
                else job.request.record_id
            )
        # The durable idempotency namespace is project/job scoped. Adapter and
        # authority context remain in the hashed parameters so a context switch
        # with the same logical key is rejected instead of silently reconciled.
        key_material = {
            "project_id": self.project.config.project_id,
            "job_id": job.job_id,
            "logical_key": caller_key,
        }
        request["idempotency_key"] = "project:" + sha256_ref(
            canonical_json(key_material)
        ).removeprefix("sha256:")
        request["_fliorcie_scope"] = {
            "adapter": job.adapter,
            "principal_id": self.project.config.actor.principal_id,
            "policy_id": self.project.config.authority.policy_id,
            "contract_id": (job.contract or self.project.config.contract).contract_id,
            "risk_tier": job.risk_tier.value,
            "approval_id": job.approval_id,
            "delegation_depth": job.delegation_depth,
        }
        return request

    @staticmethod
    def _outcome(status: EffectStatus | None) -> RunOutcome:
        if status == EffectStatus.VERIFIED:
            return "completed"
        if status == EffectStatus.COMPENSATED:
            return "recovered"
        if status in {EffectStatus.UNKNOWN, EffectStatus.MANUAL_REVIEW}:
            return "unresolved"
        return "failed"

    async def plan(self, job_id: str) -> EffectPlan:
        """Explain one validated job without constructing an adapter or journal."""
        job = self._job(job_id)
        return await EffectPlanner().plan(
            effect_name=job.adapter,
            authority=self._authority(job),
            contract=self._contract(job),
            values=self._budget_values(job),
            request=self._request(job),
        )

    async def plan_all(self) -> tuple[EffectPlan, ...]:
        """Explain every job without creating runtime state."""
        return tuple([await self.plan(job.job_id) for job in self.project.config.jobs])

    async def run(self, job_id: str) -> ProjectRunResult:
        job = self._job(job_id)
        adapter = self._adapter(job.adapter)
        authority = self._authority(job)
        run_id = generate_uuidv7()
        context = EffectContext(
            run_id=run_id,
            trace_id=new_trace_id(),
            actor=Actor(
                agent_id=self.project.config.actor.agent_id,
                principal_id=self.project.config.actor.principal_id,
            ),
            authority=authority,
            contract=self._contract(job),
            values=self._budget_values(job),
        )
        request = self._request(job)
        attempts = 1
        execution: EffectExecution | None = None
        detail: str | None = None
        rejected = False

        refund_adapter = adapter if isinstance(adapter, InMemoryRefundEffect) else None
        if isinstance(job, RefundJob) and job.simulate_response_loss:
            assert refund_adapter is not None
            refund_adapter.chaos = ChaosEngine(
                FaultInjector({FaultPoint.RESPONSE_LOSS})
            )

        with SQLiteJournalStore(self.project.journal_path) as journal:
            runner = SafeEffectRunner(journal)
            try:
                execution = await runner.run(adapter, request, context)
                if (
                    isinstance(job, RefundJob)
                    and job.simulate_response_loss
                    and execution.state == EffectStatus.UNKNOWN
                ):
                    assert refund_adapter is not None
                    refund_adapter.chaos = ChaosEngine()
                    attempts += 1
                    execution = await runner.run(adapter, request, context)
            except (EffectRejected, JournalConflict) as exc:
                rejected = True
                detail = f"{type(exc).__name__}: {exc}"

            attempt_entries = journal.scan(run_id)
            owning_run_id = execution.run_id if execution is not None else run_id
            entries = journal.scan(owning_run_id)
            verification = journal.verify(owning_run_id)
            attempt_verification = journal.verify(run_id)
            last_status = (
                EffectStatus(entries[-1].envelope["observation"]["status"])
                if entries
                else None
            )
            status: EffectStatus | None
            effect_id: str | None
            if execution is not None:
                status = execution.state
                effect_id = execution.effect_id
                deduplicated = execution.deduplicated
                external_refs = execution.external_refs
            else:
                status = last_status
                effect_id = str(entries[0].envelope["event_id"]) if entries else None
                deduplicated = False
                external_refs = ()

            outcome: RunOutcome = "rejected" if rejected else self._outcome(status)
            if status == EffectStatus.VERIFIED:
                terminal = next(
                    (
                        entry
                        for entry in reversed(entries)
                        if entry.envelope["observation"]["status"] == "verified"
                    ),
                    None,
                )
                terminal_is_bound = bool(
                    terminal
                    and terminal.envelope.get("evidence")
                    and terminal.envelope["observation"].get("verifier")
                )
                prior_run_is_linked = bool(
                    execution is not None
                    and execution.deduplicated
                    and execution.run_id != run_id
                )
                if not terminal_is_bound or (
                    not prior_run_is_linked
                    and not self._requested_state_exists(job, adapter)
                ):
                    outcome = "failed"
                    detail = "verified journal evidence did not prove the requested local state"
                elif prior_run_is_linked:
                    detail = (
                        "deduplicated result is linked to its prior evidence-owning run"
                    )
            if not verification.valid or not attempt_verification.valid:
                outcome = "failed"
                detail = (
                    verification.error
                    or attempt_verification.error
                    or "local journal verification failed"
                )

            return ProjectRunResult(
                project_id=self.project.config.project_id,
                job_id=job.job_id,
                adapter=job.adapter,
                run_id=owning_run_id,
                attempt_run_id=(run_id if owning_run_id != run_id else None),
                effect_id=effect_id,
                effect_status=status,
                outcome=outcome,
                attempts=attempts,
                dispatch_count=sum(
                    entry.event_type == "effect.executing" for entry in entries
                ),
                deduplicated=deduplicated,
                external_refs=external_refs,
                journal_path=self.project.journal_path.relative_to(
                    self.project.root
                ).as_posix(),
                journal_valid=verification.valid and attempt_verification.valid,
                journal_entries=len(entries),
                attempt_journal_entries=len(attempt_entries),
                detail=detail,
            )

    async def run_all(self) -> ProjectBatchResult:
        """Run each configured job sequentially as an independent safe effect."""
        results = tuple(
            [await self.run(job.job_id) for job in self.project.config.jobs]
        )
        outcomes = {item.outcome for item in results}
        if outcomes == {"completed"}:
            outcome: RunOutcome = "completed"
        elif "unresolved" in outcomes:
            outcome = "unresolved"
        elif "failed" in outcomes:
            outcome = "failed"
        elif "rejected" in outcomes:
            outcome = "rejected"
        else:
            outcome = "recovered"
        return ProjectBatchResult(
            project_id=self.project.config.project_id,
            outcome=outcome,
            results=results,
        )


async def run_project_job(path: str | Path, job_id: str) -> ProjectRunResult:
    """Load a project and execute one named, local-only reference job."""
    return await ProjectRunner.from_file(path).run(job_id)


async def run_project(path: str | Path) -> ProjectBatchResult:
    """Load a project and run all local reference jobs sequentially."""
    return await ProjectRunner.from_file(path).run_all()
