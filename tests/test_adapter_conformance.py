from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from llmwitness.adapter_conformance import (
    render_adapter_conformance_markdown,
    run_adapter_conformance,
    write_adapter_conformance_reports,
)
from llmwitness.adapters import (
    SUPPORTED_MAPPING_ADAPTERS,
    AdapterRegistry,
    EnvelopeAdapter,
    MappingEnvelopeAdapter,
    default_adapter_registry,
)
from llmwitness.cli import main

EXPECTED_ADAPTERS = tuple(sorted(SUPPORTED_MAPPING_ADAPTERS))
OPTIONAL_MODULES = {
    "autogen",
    "crewai",
    "langchain",
    "langgraph",
    "llama_index",
    "mcp",
    "openai",
    "opentelemetry",
}


def test_default_inventory_is_exact_and_includes_langchain() -> None:
    registry = default_adapter_registry()

    assert registry.names() == EXPECTED_ADAPTERS
    assert "langchain" in registry.names()
    assert len(registry.names()) == len(set(registry.names())) == 9


def test_every_reference_mapping_adapter_passes_all_dimensions() -> None:
    report = run_adapter_conformance()

    assert report.sample_count == 9
    assert report.passed == 9
    assert report.failed == 0
    assert tuple(result.adapter_name for result in report.results) == EXPECTED_ADAPTERS
    assert all(result.passed for result in report.results)
    assert all(len(result.dimensions) == 5 for result in report.results)
    assert all(
        dimension.passed for result in report.results for dimension in result.dimensions
    )
    assert "does not import or certify" in report.limitations


def test_conformance_does_not_import_optional_frameworks() -> None:
    before = set(sys.modules)

    report = run_adapter_conformance()

    after = set(sys.modules)
    newly_imported_roots = {name.split(".", 1)[0] for name in after - before}
    assert report.failed == 0
    assert newly_imported_roots.isdisjoint(OPTIONAL_MODULES)


class SemanticDriftAdapter:
    name = "semantic-drift"

    def translate(self, event: Mapping[str, Any]):
        changed = dict(event)
        changed["intent"] = "different.intent"
        return MappingEnvelopeAdapter(self.name).translate(changed)


class SecretRaisingAdapter:
    name = "secret-raising"

    def translate(self, event: Mapping[str, Any]):
        raise RuntimeError("token sk-secret-must-not-enter-report")


def _registry_with(adapter_name: str, adapter: EnvelopeAdapter) -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register("python", lambda: MappingEnvelopeAdapter("python"))
    registry.register(adapter_name, lambda: adapter)
    return registry


def test_semantic_drift_fails_only_relevant_dimensions() -> None:
    report = run_adapter_conformance(
        _registry_with("semantic-drift", SemanticDriftAdapter())
    )
    result = next(
        item for item in report.results if item.adapter_name == "semantic-drift"
    )
    dimensions = {item.name: item.passed for item in result.dimensions}

    assert report.failed == 1
    assert result.passed is False
    assert dimensions["translation"] is True
    assert dimensions["uuidv7_correlation"] is True
    assert dimensions["semantic_equivalence"] is False
    assert dimensions["parameter_digest"] is True
    assert dimensions["otel_attributes"] is False


def test_adapter_exception_is_type_only_and_secret_safe() -> None:
    report = run_adapter_conformance(
        _registry_with("secret-raising", SecretRaisingAdapter())
    )
    serialized = report.model_dump_json()
    result = next(
        item for item in report.results if item.adapter_name == "secret-raising"
    )

    assert result.passed is False
    assert result.dimensions[0].detail == "translation raised RuntimeError"
    assert "sk-secret" not in serialized


def test_reports_are_deterministic_and_contain_no_duration_or_score(
    tmp_path: Path,
) -> None:
    report = run_adapter_conformance()

    json_path, markdown_path = write_adapter_conformance_reports(report, tmp_path)
    expected_json = json_path.read_text(encoding="utf-8")
    expected_markdown = markdown_path.read_text(encoding="utf-8")
    write_adapter_conformance_reports(report, tmp_path)

    assert json_path.read_text(encoding="utf-8") == expected_json
    assert markdown_path.read_text(encoding="utf-8") == expected_markdown
    assert expected_markdown == render_adapter_conformance_markdown(report)
    assert "duration" not in expected_json.lower()
    assert "score" not in expected_json.lower()


def test_cli_writes_reports_and_explains_claim_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "reports"

    main(["adapter-conformance", "--output-dir", str(output)])

    console = capsys.readouterr().out
    assert "[OK] 9/9 mapping adapters passed" in console
    assert "does not certify a real framework SDK" in console
    assert (output / "adapter-conformance.json").is_file()
    assert (output / "adapter-conformance.md").is_file()
    payload = json.loads(
        (output / "adapter-conformance.json").read_text(encoding="utf-8")
    )
    assert payload["failed"] == 0


def test_missing_python_baseline_fails_closed_without_running_adapters() -> None:
    registry = AdapterRegistry()
    registry.register("other", lambda: MappingEnvelopeAdapter("other"))

    report = run_adapter_conformance(registry)

    assert report.passed == 0
    assert report.failed == 1
    assert report.results[0].dimensions[0].name == "semantic_equivalence"
    assert report.results[0].dimensions[0].passed is False
