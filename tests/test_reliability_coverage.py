"""Explicit coverage for high-risk reliability corpus gaps."""

import json

from llmwitness.reliability import run_reliability_corpus, write_reports


def test_disagreement_and_poisoning_scenarios_fail_closed():
    results = {result.name: result for result in run_reliability_corpus()}

    disagreement = results["verifier_disagreement"]
    poisoning = results["tool_output_poisoning"]
    rate_limited = results["provider_rate_limited_before_dispatch"]
    assert disagreement.passed
    assert disagreement.invariant == "conflicting verification cannot produce VERIFIED"
    assert poisoning.passed
    assert poisoning.invariant == (
        "untrusted tool output cannot self-assert VERIFIED or enter the journal"
    )
    assert rate_limited.passed
    assert rate_limited.invariant == (
        "a definitively rejected request cannot create or verify an effect"
    )


def test_new_scenarios_are_present_in_every_report_format(tmp_path):
    results = run_reliability_corpus()
    json_path = tmp_path / "reliability.json"
    junit_path = tmp_path / "reliability.junit.xml"
    markdown_path = tmp_path / "reliability.md"

    write_reports(
        results,
        json_path=json_path,
        junit_path=junit_path,
        markdown_path=markdown_path,
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["sample_count"] == 15
    assert payload["failed"] == 0
    assert {item["name"] for item in payload["dimensions"]} >= {
        "verifier_disagreement",
        "tool_output_poisoning",
        "provider_rate_limited_before_dispatch",
    }

    junit = junit_path.read_text(encoding="utf-8")
    assert '<testsuite name="fliorcie-reliability" tests="15" failures="0">' in junit
    assert '<testcase name="verifier_disagreement"' in junit
    assert '<testcase name="tool_output_poisoning"' in junit
    assert '<testcase name="provider_rate_limited_before_dispatch"' in junit

    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Scenarios: 15; passed: 15; failed: 0." in markdown
    assert "`verifier_disagreement`" in markdown
    assert "`tool_output_poisoning`" in markdown
    assert "`provider_rate_limited_before_dispatch`" in markdown
    assert "not a universal safety, production, or compliance score" in markdown
