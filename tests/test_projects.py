import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from llmwitness.envelope import EffectStatus
from llmwitness.journal import SQLiteJournalStore
from llmwitness.projects import (
    ProjectConfig,
    ProjectRunner,
    bootstrap_project,
    load_project,
)

SAMPLES = Path(__file__).parents[1] / "examples" / "projects"


def test_checked_in_project_schema_matches_runtime_model():
    schema_path = (
        Path(__file__).parents[1] / "schemas" / "fliorcie-project-v0.1.schema.json"
    )
    assert json.loads(schema_path.read_text(encoding="utf-8")) == (
        ProjectConfig.model_json_schema()
    )


def write_sample(tmp_path: Path, name: str, mutate=None) -> Path:
    value = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    if mutate is not None:
        mutate(value)
    destination = tmp_path / f"{name}.json"
    destination.write_text(json.dumps(value), encoding="utf-8")
    return destination


def test_bootstrap_and_run_all_create_a_bounded_reference_project(tmp_path):
    destination = tmp_path / "starter"
    config_path = bootstrap_project(destination)

    assert config_path == destination / "fliorcie.project.json"
    assert (destination / ".gitignore").read_text(encoding="utf-8") == ".llmwitness/\n"
    assert not (destination / ".llmwitness").exists()
    loaded = load_project(config_path)
    assert len(loaded.config.jobs) == 2
    assert ProjectConfig.model_json_schema()["title"] == "ProjectConfig"

    result = asyncio.run(ProjectRunner(loaded).run_all())

    assert result.outcome == "completed"
    assert [item.effect_status for item in result.results] == [
        EffectStatus.VERIFIED,
        EffectStatus.VERIFIED,
    ]
    assert [item.dispatch_count for item in result.results] == [1, 1]
    assert all(item.journal_valid for item in result.results)
    with pytest.raises(FileExistsError, match="empty"):
        bootstrap_project(destination)


def test_refund_project_reconciles_response_loss_without_duplicate(tmp_path):
    project_path = write_sample(tmp_path, "refund")
    runner = ProjectRunner.from_file(project_path)

    result = asyncio.run(runner.run("refund-payment"))

    assert result.outcome == "completed"
    assert result.effect_status == EffectStatus.VERIFIED
    assert result.attempts == 2
    assert result.dispatch_count == 1
    assert result.deduplicated
    assert result.journal_valid
    assert uuid.UUID(result.run_id).version == 7
    assert result.effect_id is not None and uuid.UUID(result.effect_id).version == 7
    assert runner.project.journal_path.parent == tmp_path / ".llmwitness"
    with SQLiteJournalStore(runner.project.journal_path) as journal:
        event_types = [entry.event_type for entry in journal.scan(result.run_id)]
    assert event_types.count("effect.executing") == 1
    assert event_types.count("effect.unknown") == 1
    assert event_types.count("effect.verified") == 1


def test_crm_project_runs_only_the_local_reference_effect(tmp_path):
    project_path = write_sample(tmp_path, "crm")

    result = asyncio.run(ProjectRunner.from_file(project_path).run("update-customer"))

    assert result.outcome == "completed"
    assert result.adapter == "reference.crm-update"
    assert result.effect_status == EffectStatus.VERIFIED
    assert result.dispatch_count == 1
    assert result.external_refs == ("customer-demo-001",)


@pytest.mark.parametrize(
    "unsafe_path",
    ["../outside.db", ".llmwitness/../../outside.db"],
)
def test_project_paths_reject_traversal_before_creating_state(tmp_path, unsafe_path):
    project_path = write_sample(
        tmp_path,
        "refund",
        lambda value: value["journal"].update(path=unsafe_path),
    )

    with pytest.raises((ValidationError, ValueError), match=r"\.\.|project"):
        load_project(project_path)

    assert not (tmp_path / ".llmwitness").exists()


def test_project_path_rejects_absolute_location_before_creating_state(tmp_path):
    outside = tmp_path.parent / "outside.db"
    project_path = write_sample(
        tmp_path,
        "refund",
        lambda value: value["journal"].update(path=str(outside)),
    )

    with pytest.raises(ValidationError, match="project-relative"):
        load_project(project_path)

    assert not (tmp_path / ".llmwitness").exists()


def test_project_path_rejects_storage_symlink_escape(tmp_path):
    project_path = write_sample(tmp_path, "refund")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    try:
        os.symlink(outside, tmp_path / ".llmwitness", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this environment")

    with pytest.raises(ValueError, match="escapes the project root"):
        load_project(project_path)


def test_unknown_or_dynamic_adapter_is_rejected_by_strict_config(tmp_path):
    project_path = write_sample(
        tmp_path,
        "refund",
        lambda value: value["jobs"][0].update(adapter="https://example.test/effect"),
    )

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        load_project(project_path)

    assert not (tmp_path / ".llmwitness").exists()


def test_r3_requires_nonblank_explicit_approval(tmp_path):
    def make_r3(value):
        value["authority"]["maximum_risk"] = "R3"
        value["jobs"][0]["risk_tier"] = "R3"
        value["jobs"][0]["simulate_response_loss"] = False

    project_path = write_sample(tmp_path, "refund", make_r3)
    denied = asyncio.run(ProjectRunner.from_file(project_path).run("refund-payment"))

    assert denied.outcome == "rejected"
    assert denied.effect_status == EffectStatus.PLANNED
    assert denied.dispatch_count == 0
    assert "require_approval" in (denied.detail or "")

    value = json.loads(project_path.read_text(encoding="utf-8"))
    value["jobs"][0]["approval_id"] = "approval-local-001"
    project_path.write_text(json.dumps(value), encoding="utf-8")
    approved = asyncio.run(ProjectRunner.from_file(project_path).run("refund-payment"))
    assert approved.outcome == "completed"
    assert approved.effect_status == EffectStatus.VERIFIED
    assert approved.dispatch_count == 1

    value["jobs"][0]["approval_id"] = " "
    project_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValidationError, match="approval_id must not be blank"):
        load_project(project_path)


def test_r2_derived_budget_fails_closed_before_dispatch(tmp_path):
    project_path = write_sample(
        tmp_path,
        "refund",
        lambda value: value["jobs"][0].update(
            request={"payment_id": "payment-demo-001", "amount": 500},
            simulate_response_loss=False,
        ),
    )

    result = asyncio.run(ProjectRunner.from_file(project_path).run("refund-payment"))

    assert result.outcome == "rejected"
    assert result.effect_status == EffectStatus.FAILED
    assert result.dispatch_count == 0
    assert result.journal_valid


def test_same_logical_key_is_isolated_by_reference_adapter_namespace(tmp_path):
    def first_job(value):
        value["jobs"][0]["job_id"] = "shared-job"
        value["jobs"][0]["request"]["idempotency_key"] = "logical-001"
        value["jobs"][0]["simulate_response_loss"] = False

    project_path = write_sample(tmp_path, "refund", first_job)
    first = asyncio.run(ProjectRunner.from_file(project_path).run("shared-job"))
    assert first.outcome == "completed"

    value = json.loads(project_path.read_text(encoding="utf-8"))
    value["authority"]["allowed_effects"] = ["reference.crm-update"]
    value["contract"] = {
        "contract_id": "changed-contract",
        "budgets": {"record_updates": 1},
    }
    value["jobs"] = [
        {
            "job_id": "shared-job",
            "adapter": "reference.crm-update",
            "risk_tier": "R2",
            "request": {
                "record_id": "customer-001",
                "fields": {"status": "changed"},
                "idempotency_key": "logical-001",
            },
        }
    ]
    project_path.write_text(json.dumps(value), encoding="utf-8")

    switched = asyncio.run(ProjectRunner.from_file(project_path).run("shared-job"))

    assert switched.outcome == "completed"
    assert switched.effect_status == EffectStatus.VERIFIED
    assert switched.dispatch_count == 1
    assert switched.journal_valid


def test_raw_request_secret_is_rejected_before_local_journal(tmp_path):
    secret = "sk-example-project-secret-value-123456"

    def add_secret(value):
        value["jobs"][0]["request"]["fields"]["api_key"] = secret

    project_path = write_sample(tmp_path, "crm", add_secret)

    with pytest.raises(ValidationError, match="best-effort pattern scrubbing"):
        ProjectRunner.from_file(project_path)
    assert not (tmp_path / ".llmwitness").exists()


def test_new_run_cannot_reuse_prior_scope_or_dispatch_duplicate(tmp_path):
    project_path = write_sample(
        tmp_path,
        "refund",
        lambda value: value["jobs"][0].update(simulate_response_loss=False),
    )
    first = asyncio.run(ProjectRunner.from_file(project_path).run("refund-payment"))
    second = asyncio.run(ProjectRunner.from_file(project_path).run("refund-payment"))

    assert first.outcome == "completed"
    assert second.outcome == "rejected"
    assert second.run_id != first.run_id
    assert second.attempt_run_id is None
    assert not second.deduplicated
    assert second.dispatch_count == 0
    assert second.attempt_journal_entries > 0
    assert "execution scope" in (second.detail or "")
