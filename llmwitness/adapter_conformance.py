"""Deterministic conformance for dependency-free framework mapping adapters."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from llmwitness.adapters import (
    SUPPORTED_MAPPING_ADAPTERS,
    AdapterRegistry,
    EnvelopeTranslator,
    default_adapter_registry,
    envelope_to_otel_attributes,
)
from llmwitness.envelope import ExecutionEnvelope, sha256_ref
from llmwitness.utils import canonical_json

_RUN_ID = "12345678-1234-7123-8123-456789012345"
_EVENT_ID = "22345678-1234-7123-8123-456789012345"
_TRACE_ID = "0123456789abcdef0123456789abcdef"
_PARAMETERS = {"record_id": "record-001", "value": 7}
_LIMITATIONS = (
    "This deterministic result covers normalized mapping translation only. It "
    "does not import or certify a framework SDK, native hook, orchestration "
    "workflow, provider effect, external repository, or production behavior."
)
DimensionName = Literal[
    "translation",
    "uuidv7_correlation",
    "semantic_equivalence",
    "parameter_digest",
    "otel_attributes",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdapterConformanceDimension(_FrozenModel):
    name: DimensionName
    passed: bool
    detail: str


class AdapterConformanceResult(_FrozenModel):
    adapter_name: str
    passed: bool
    dimensions: tuple[AdapterConformanceDimension, ...]


class AdapterConformanceReport(_FrozenModel):
    schema_version: Literal["0.1"] = "0.1"
    fixture_id: Literal["mapping-equivalence-v0.1"] = "mapping-equivalence-v0.1"
    sample_count: int
    passed: int
    failed: int
    results: tuple[AdapterConformanceResult, ...]
    limitations: str = _LIMITATIONS


def _fixture() -> dict[str, Any]:
    return {
        "run_id": _RUN_ID,
        "event_id": _EVENT_ID,
        "trace_id": _TRACE_ID,
        "agent_id": "conformance-agent",
        "principal_id": "local-conformance-user",
        "intent": "record.lookup",
        "reason": "deterministic mapping conformance",
        "policy_id": "conformance-policy-v0.1",
        "authority_decision": "allow",
        "risk_tier": "R1",
        "contract_id": "conformance-contract-v0.1",
        "preconditions": ["record identifier is present"],
        "invariants": ["correlation is preserved"],
        "postconditions": ["mapping remains equivalent"],
        "budgets": {"calls": 1},
        "pre_state_ref": sha256_ref("before"),
        "post_state_ref": sha256_ref("after"),
        "effect_adapter": "record.lookup",
        "idempotency_key": "record-001:lookup",
        "parameters": dict(_PARAMETERS),
        "status": "planned",
        "compensation_available": False,
        "external_refs": [],
        "evidence": [],
    }


def _semantic_projection(envelope: ExecutionEnvelope) -> Mapping[str, Any]:
    projection = envelope.model_dump(mode="json")
    projection.pop("created_at", None)
    return projection


def _dimension(
    name: DimensionName,
    passed: bool,
    success: str,
    failure: str,
) -> AdapterConformanceDimension:
    return AdapterConformanceDimension(
        name=name, passed=passed, detail=success if passed else failure
    )


def _exception_result(adapter_name: str, exc: Exception) -> AdapterConformanceResult:
    dimension = AdapterConformanceDimension(
        name="translation",
        passed=False,
        detail=f"translation raised {type(exc).__name__}",
    )
    return AdapterConformanceResult(
        adapter_name=adapter_name,
        passed=False,
        dimensions=(dimension,),
    )


def _evaluate_adapter(
    translator: EnvelopeTranslator,
    adapter_name: str,
    baseline: ExecutionEnvelope,
) -> AdapterConformanceResult:
    try:
        envelope = translator.translate(adapter_name, _fixture())
    except Exception as exc:
        return _exception_result(adapter_name, exc)

    expected_digest = sha256_ref(canonical_json(_PARAMETERS))
    dimensions = (
        AdapterConformanceDimension(
            name="translation", passed=True, detail="translation completed"
        ),
        _dimension(
            "uuidv7_correlation",
            envelope.run_id == _RUN_ID and envelope.event_id == _EVENT_ID,
            "supplied run and event UUIDv7 identifiers were preserved",
            "supplied run or event UUIDv7 identifier changed",
        ),
        _dimension(
            "semantic_equivalence",
            _semantic_projection(envelope) == _semantic_projection(baseline),
            "stable envelope semantics match the Python mapping baseline",
            "stable envelope semantics differ from the Python mapping baseline",
        ),
        _dimension(
            "parameter_digest",
            envelope.effect.parameters_hash == expected_digest,
            "canonical parameter digest matches",
            "canonical parameter digest differs",
        ),
        _dimension(
            "otel_attributes",
            envelope_to_otel_attributes(envelope)
            == envelope_to_otel_attributes(baseline),
            "stable OTel attributes match the baseline",
            "stable OTel attributes differ from the baseline",
        ),
    )
    return AdapterConformanceResult(
        adapter_name=adapter_name,
        passed=all(dimension.passed for dimension in dimensions),
        dimensions=dimensions,
    )


def run_adapter_conformance(
    registry: AdapterRegistry | None = None,
) -> AdapterConformanceReport:
    """Run O(n) deterministic mapping conformance for registered adapters."""

    active_registry = registry or default_adapter_registry()
    translator = EnvelopeTranslator(active_registry)
    names = active_registry.names()
    try:
        baseline = translator.translate("python", _fixture())
    except Exception as exc:
        baseline_failure = _exception_result("python", exc)
        results = tuple(
            (
                baseline_failure
                if name == "python"
                else AdapterConformanceResult(
                    adapter_name=name,
                    passed=False,
                    dimensions=(
                        AdapterConformanceDimension(
                            name="semantic_equivalence",
                            passed=False,
                            detail="Python mapping baseline is unavailable",
                        ),
                    ),
                )
            )
            for name in names
        )
    else:
        results = tuple(
            _evaluate_adapter(translator, adapter_name, baseline)
            for adapter_name in names
        )
    passed = sum(result.passed for result in results)
    return AdapterConformanceReport(
        sample_count=len(results),
        passed=passed,
        failed=len(results) - passed,
        results=results,
    )


def render_adapter_conformance_markdown(report: AdapterConformanceReport) -> str:
    status = "PASS" if report.failed == 0 else "FAIL"
    lines = [
        "# Framework Mapping-Adapter Conformance",
        "",
        f"- Fixture: `{report.fixture_id}`",
        f"- Result: **{status}**",
        f"- Adapters: {report.passed}/{report.sample_count} passed",
        "",
        report.limitations,
        "",
        "## Results",
        "",
    ]
    for result in report.results:
        lines.append(
            f"### `{result.adapter_name}` — {'PASS' if result.passed else 'FAIL'}"
        )
        lines.append("")
        lines.extend(
            f"- `{dimension.name}`: {'PASS' if dimension.passed else 'FAIL'} — {dimension.detail}"
            for dimension in result.dimensions
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_adapter_conformance_reports(
    report: AdapterConformanceReport, output_directory: str | Path
) -> tuple[Path, Path]:
    output = Path(output_directory)
    json_path = output / "adapter-conformance.json"
    markdown_path = output / "adapter-conformance.md"
    serialized = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)
    _atomic_write(json_path, serialized + "\n")
    _atomic_write(markdown_path, render_adapter_conformance_markdown(report))
    return json_path, markdown_path


__all__ = [
    "AdapterConformanceDimension",
    "AdapterConformanceReport",
    "AdapterConformanceResult",
    "SUPPORTED_MAPPING_ADAPTERS",
    "render_adapter_conformance_markdown",
    "run_adapter_conformance",
    "write_adapter_conformance_reports",
]
