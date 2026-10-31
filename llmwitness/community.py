"""Typed composition facade for the complete local Community workflow.

This module sequences existing owners. It does not implement authority,
contracts, effects, recovery execution, replay, or reliability semantics.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from llmwitness.journal import SQLiteJournalStore
from llmwitness.projects import (
    LoadedProject,
    ProjectBatchResult,
    ProjectRunner,
    load_project,
)
from llmwitness.recovery import RecoveryItem, RecoveryPlanner
from llmwitness.reliability import (
    ReliabilityReporter,
    ScenarioResult,
    run_reliability_corpus,
)


class _ReliabilityReportWriter(Protocol):
    def report(
        self, results: list[ScenarioResult], *, output_dir: Path
    ) -> dict[str, Any]: ...


class CommunityReportPaths(BaseModel):
    """Local files emitted from one SDK reliability run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    output_dir: Path
    json_path: Path
    junit_path: Path
    markdown_path: Path


class CommunityWorkflowResult(BaseModel):
    """Typed results from existing project, recovery, and reliability owners."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["0.1"] = "0.1"
    project: ProjectBatchResult
    recovery_items: tuple[RecoveryItem, ...]
    reliability: dict[str, Any]
    reports: CommunityReportPaths


class CommunitySDK:
    """Run the complete local reference workflow through existing components."""

    def __init__(
        self,
        *,
        reliability_runner: Callable[[], list[ScenarioResult]] | None = None,
        reliability_reporter: _ReliabilityReportWriter | None = None,
    ) -> None:
        self._reliability_runner = (
            reliability_runner
            if reliability_runner is not None
            else run_reliability_corpus
        )
        self._reliability_reporter = (
            reliability_reporter
            if reliability_reporter is not None
            else ReliabilityReporter()
        )

    @staticmethod
    def _report_directory(
        project: LoadedProject, report_dir: str | Path | None
    ) -> Path:
        if report_dir is None:
            repository_local = (
                project.root / ".llmwitness" / "community-reliability"
            ).resolve(strict=False)
            try:
                repository_local.relative_to(project.root)
            except ValueError as exc:
                raise ValueError(
                    "default Community report directory escapes the project root"
                ) from exc
            selected = repository_local
        else:
            requested = Path(report_dir).expanduser()
            if requested.exists() and requested.is_symlink():
                raise ValueError("Community report directory must not be a symlink")
            selected = requested.resolve(strict=False)

        if selected.exists() and not selected.is_dir():
            raise ValueError("Community report destination must be a directory")
        selected.mkdir(parents=True, exist_ok=True)
        return selected

    async def run(
        self,
        project_config: str | Path,
        *,
        report_dir: str | Path | None = None,
        include_terminal_recovery: bool = False,
    ) -> CommunityWorkflowResult:
        """Run validated local effects, inspect recovery, and emit CI reports."""
        project = load_project(project_config)
        output_dir = self._report_directory(project, report_dir)

        scenario_results = await asyncio.to_thread(self._reliability_runner)
        reliability = await asyncio.to_thread(
            self._reliability_reporter.report,
            scenario_results,
            output_dir=output_dir,
        )

        project_result = await ProjectRunner(project).run_all()

        with SQLiteJournalStore(project.journal_path, read_only=True) as journal:
            recovery_items = RecoveryPlanner(journal).list_items(
                include_terminal=include_terminal_recovery
            )

        reports = CommunityReportPaths(
            output_dir=output_dir,
            json_path=output_dir / "reliability.json",
            junit_path=output_dir / "reliability.junit.xml",
            markdown_path=output_dir / "reliability.md",
        )
        return CommunityWorkflowResult(
            project=project_result,
            recovery_items=recovery_items,
            reliability=reliability,
            reports=reports,
        )

    def run_sync(
        self,
        project_config: str | Path,
        *,
        report_dir: str | Path | None = None,
        include_terminal_recovery: bool = False,
    ) -> CommunityWorkflowResult:
        """Run from synchronous code; async callers must await :meth:`run`."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.run(
                    project_config,
                    report_dir=report_dir,
                    include_terminal_recovery=include_terminal_recovery,
                )
            )
        raise RuntimeError(
            "CommunitySDK.run_sync cannot run inside an active event loop; "
            "await CommunitySDK.run instead"
        )


__all__ = [
    "CommunityReportPaths",
    "CommunitySDK",
    "CommunityWorkflowResult",
]
