import asyncio
import json
import sys

from llmwitness.authority import AuthorityResult
from llmwitness.cli import main
from llmwitness.contracts import (
    AgentContract,
    ContractCheck,
    EvaluationClass,
)
from llmwitness.envelope import AuthorityDecision, RiskTier
from llmwitness.planning import EffectPlanner
from llmwitness.projects import ProjectRunner, bootstrap_project


def _authority(
    decision: AuthorityDecision = AuthorityDecision.ALLOW,
    risk: RiskTier = RiskTier.R2,
) -> AuthorityResult:
    return AuthorityResult("policy", decision, risk, "test decision")


def test_project_plan_reuses_project_semantics_without_adapter_or_journal(
    tmp_path, monkeypatch
):
    config_path = bootstrap_project(tmp_path / "planned-project")
    runner = ProjectRunner.from_file(config_path)

    def forbidden_adapter(_name):
        raise AssertionError("planning must not construct a provider adapter")

    monkeypatch.setattr(runner, "_adapter", forbidden_adapter)
    plan = asyncio.run(runner.plan("refund-payment"))

    assert plan.allowed
    assert plan.next_safe_action == "execute"
    assert plan.effect_name == "reference.refund"
    assert plan.request_hash.startswith("sha256:")
    assert plan.values_hash.startswith("sha256:")
    assert not (config_path.parent / ".llmwitness").exists()
    assert runner._adapters == {}
    serialized = json.dumps(plan.model_dump(mode="json"))
    assert "payment-demo-001" not in serialized
    assert '"amount": 25' not in serialized


def test_planner_explains_approval_authority_and_contract_failures():
    planner = EffectPlanner()
    approval = asyncio.run(
        planner.plan(
            effect_name="refund",
            authority=_authority(AuthorityDecision.REQUIRE_APPROVAL, RiskTier.R3),
            contract=AgentContract("contract"),
            values={},
            request={"payment": "p-1"},
        )
    )
    assert not approval.allowed
    assert approval.next_safe_action == "request_approval"

    denied = asyncio.run(
        planner.plan(
            effect_name="refund",
            authority=_authority(AuthorityDecision.DENY),
            contract=AgentContract("contract"),
            values={},
            request={"payment": "p-1"},
        )
    )
    assert not denied.allowed
    assert denied.next_safe_action == "revise_authority"

    budget = asyncio.run(
        planner.plan(
            effect_name="refund",
            authority=_authority(),
            contract=AgentContract("contract", budgets={"amount": 10}),
            values={"amount": 11},
            request={"payment": "p-1"},
        )
    )
    assert not budget.allowed
    assert budget.next_safe_action == "revise_contract"
    assert any(item.name == "budget:amount" for item in budget.preconditions)
    assert all(item.detail is None for item in budget.preconditions)

    forged_allow = asyncio.run(
        planner.plan(
            effect_name="refund",
            authority=_authority(AuthorityDecision.ALLOW, RiskTier.R3),
            contract=AgentContract("contract"),
            values={},
            request={"payment": "p-1"},
        )
    )
    assert not forged_allow.allowed
    assert forged_allow.next_safe_action == "request_approval"


def test_planner_preserves_model_only_high_risk_fail_closed_and_pending_postconditions():
    contract = AgentContract(
        "contract",
        preconditions=(
            ContractCheck(
                "model-opinion",
                EvaluationClass.MODEL_ASSISTED,
                lambda _values: True,
            ),
        ),
        postconditions=(
            ContractCheck(
                "provider-state-matches",
                EvaluationClass.EXTERNAL,
                lambda _values: True,
            ),
        ),
    )

    plan = asyncio.run(
        EffectPlanner().plan(
            effect_name="refund",
            authority=_authority(risk=RiskTier.R2),
            contract=contract,
            values={},
            request={"payment": "p-1"},
        )
    )

    assert not plan.allowed
    assert plan.next_safe_action == "revise_contract"
    assert plan.postconditions_pending == ("provider-state-matches",)


def test_plan_contains_digests_not_raw_secret_values():
    raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"
    plan = asyncio.run(
        EffectPlanner().plan(
            effect_name="refund",
            authority=_authority(),
            contract=AgentContract("contract"),
            values={"authorization": raw_secret},
            request={"token": raw_secret},
        )
    )

    serialized = json.dumps(plan.model_dump(mode="json"))
    assert raw_secret not in serialized
    assert "authorization" not in serialized
    assert "token" not in serialized


def test_cli_plans_all_project_jobs_without_creating_runtime_state(
    tmp_path, monkeypatch, capsys
):
    config_path = bootstrap_project(tmp_path / "cli-plan")
    monkeypatch.setattr(
        sys,
        "argv",
        ["llmwitness", "project", "plan", str(config_path), "--all"],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 2
    assert all(item["allowed"] for item in payload)
    assert not (config_path.parent / ".llmwitness").exists()
