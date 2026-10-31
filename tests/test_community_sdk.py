"""End-to-end tests for the local Community SDK composition facade."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from pydantic import ValidationError

from llmwitness.community import CommunitySDK
from llmwitness.envelope import EffectStatus
from llmwitness.projects import bootstrap_project
from llmwitness.recovery import RecoveryAction
from llmwitness.reliability import ScenarioResult


def _assert_uuidv7(value: str) -> None:
    parsed = uuid.UUID(value)
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122


def test_sync_sdk_runs_complete_local_community_workflow(tmp_path):
    config_path = bootstrap_project(tmp_path / "community-project")

    result = CommunitySDK().run_sync(config_path)

    assert result.project.outcome == "completed"
    assert len(result.project.results) == 2
    for job in result.project.results:
        _assert_uuidv7(job.run_id)
        assert job.effect_id is not None
        _assert_uuidv7(job.effect_id)
        assert job.effect_status == EffectStatus.VERIFIED
        assert job.journal_valid

    refund = result.project.results[0]
    assert refund.attempts == 2
    assert refund.dispatch_count == 1
    assert refund.deduplicated
    assert result.recovery_items == ()

    assert result.reliability["sample_count"] == 15
    assert result.reliability["passed"] == 15
    assert result.reliability["failed"] == 0
    assert (
        result.reports.output_dir
        == (config_path.parent / ".llmwitness" / "community-reliability").resolve()
    )
    assert result.reports.json_path.is_file()
    assert result.reports.junit_path.is_file()
    assert result.reports.markdown_path.is_file()


def test_async_sdk_can_include_terminal_recovery_evidence(tmp_path):
    config_path = bootstrap_project(tmp_path / "async-community")

    result = asyncio.run(
        CommunitySDK().run(
            config_path,
            report_dir=tmp_path / "reports",
            include_terminal_recovery=True,
        )
    )

    assert result.project.outcome == "completed"
    assert len(result.recovery_items) == 2
    assert {item.status for item in result.recovery_items} == {EffectStatus.VERIFIED}
    assert {item.action for item in result.recovery_items} == {RecoveryAction.NONE}


def test_reliability_failure_remains_visible_in_result_and_reports(tmp_path):
    config_path = bootstrap_project(tmp_path / "failed-reliability")
    failed = ScenarioResult(
        name="forced_failure",
        passed=False,
        invariant="failure remains visible",
        duration_ms=0.0,
        detail="injected test failure",
    )

    result = CommunitySDK(reliability_runner=lambda: [failed]).run_sync(config_path)

    assert result.project.outcome == "completed"
    assert result.reliability["sample_count"] == 1
    assert result.reliability["passed"] == 0
    assert result.reliability["failed"] == 1
    assert "forced_failure" in result.reports.markdown_path.read_text(encoding="utf-8")
    assert "failure" in result.reports.junit_path.read_text(encoding="utf-8")


def test_invalid_project_creates_no_report_output(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"schema_version":"0.1"}', encoding="utf-8")
    reports = tmp_path / "should-not-exist"

    with pytest.raises(ValidationError):
        CommunitySDK().run_sync(invalid, report_dir=reports)

    assert not reports.exists()


def test_invalid_report_destination_prevents_effect_execution(tmp_path):
    project_root = tmp_path / "invalid-report-project"
    config_path = bootstrap_project(project_root)
    report_file = tmp_path / "not-a-directory"
    report_file.write_text("occupied", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a directory"):
        CommunitySDK().run_sync(config_path, report_dir=report_file)

    assert not (project_root / ".llmwitness" / "journal.db").exists()


def test_report_symlink_prevents_effect_execution(tmp_path):
    project_root = tmp_path / "symlink-report-project"
    config_path = bootstrap_project(project_root)
    target = tmp_path / "report-target"
    target.mkdir()
    link = tmp_path / "report-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this environment")

    with pytest.raises(ValueError, match="must not be a symlink"):
        CommunitySDK().run_sync(config_path, report_dir=link)

    assert not (project_root / ".llmwitness" / "journal.db").exists()


def test_report_failure_happens_before_effect_execution(tmp_path):
    class FailingReporter:
        def report(self, results, *, output_dir):
            del results, output_dir
            raise OSError("report storage unavailable")

    project_root = tmp_path / "report-failure-project"
    config_path = bootstrap_project(project_root)

    with pytest.raises(OSError, match="report storage unavailable"):
        CommunitySDK(reliability_reporter=FailingReporter()).run_sync(config_path)

    assert not (project_root / ".llmwitness" / "journal.db").exists()


def test_sync_wrapper_rejects_an_active_event_loop(tmp_path):
    config_path = bootstrap_project(tmp_path / "active-loop")

    async def call_sync_wrapper() -> None:
        with pytest.raises(RuntimeError, match="await CommunitySDK.run"):
            CommunitySDK().run_sync(config_path)

    asyncio.run(call_sync_wrapper())
    assert not (config_path.parent / ".llmwitness").exists()


def test_result_is_json_serializable_without_losing_owner_outcomes(tmp_path):
    config_path = bootstrap_project(tmp_path / "serializable-result")
    result = CommunitySDK().run_sync(config_path)

    payload = json.loads(result.model_dump_json())

    assert payload["schema_version"] == "0.1"
    assert payload["project"]["outcome"] == "completed"
    assert payload["reliability"]["failed"] == 0
