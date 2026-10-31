from pathlib import Path

import httpx

from llmwitness import gateway

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_large_json_audit_uses_a_bounded_text_preview(monkeypatch):
    preview_limit = 40
    content = b'{"secret":"123-45-6789","padding":"' + (b"x" * 100) + b'"}'
    response = httpx.Response(
        200, content=content, headers={"content-type": "application/json"}
    )

    def fail_if_json_is_parsed():
        raise AssertionError("a truncated audit response must not be JSON-parsed")

    monkeypatch.setattr(gateway, "MAX_AUDIT_TEXT_BYTES", preview_limit)
    monkeypatch.setattr(response, "json", fail_if_json_is_parsed)

    audit = gateway._audit_copy(response)

    assert audit["truncated"] is True
    assert audit["body_bytes"] == len(content)
    assert audit["preview_bytes"] == preview_limit
    assert "123-45-6789" not in audit["body_preview"]


def test_audit_truncation_metadata_counts_source_bytes(monkeypatch):
    response = httpx.Response(200, content=b"\xc3\xa9")
    monkeypatch.setattr(gateway, "MAX_AUDIT_TEXT_BYTES", 1)

    audit = gateway._audit_copy(response)

    assert audit["truncated"] is True
    assert audit["body_bytes"] == 2
    assert audit["preview_bytes"] == 1


def test_sdist_manifest_excludes_generated_benchmark_results():
    generated_results = list(
        (REPOSITORY_ROOT / "benchmarks" / "results").glob("*.json")
    )
    assert generated_results, "the regression fixture must exercise generated results"

    directives = [
        line.strip()
        for line in (REPOSITORY_ROOT / "MANIFEST.in")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    include_index = directives.index("recursive-include benchmarks *.py *.md *.json")
    prune_index = directives.index("prune benchmarks/results")
    assert prune_index > include_index
