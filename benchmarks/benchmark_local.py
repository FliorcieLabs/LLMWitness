"""Reproducible local microbenchmarks for LLMWitness Community.

Results describe only the recorded machine, dependency set, payloads, and commit.
They are not service-level objectives or universal performance claims.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import httpx

from llmwitness import gateway
from llmwitness.authority import AuthorityPolicy
from llmwitness.envelope import RiskTier, sha256_ref
from llmwitness.passport import (
    InMemoryNonceReplayGuard,
    PassportAuthorityMapper,
    PassportCapability,
    PassportClaims,
    PassportInvocation,
    PassportIssuer,
    PassportStatus,
    PassportTrustAnchor,
    PassportVerifier,
)
from llmwitness.sdk import LLMWitnessTracker
from llmwitness.utils import Ed25519KeyManager, canonical_json, redact_payload


def percentile(sorted_values: list[float], fraction: float) -> float:
    index = min(len(sorted_values) - 1, int((len(sorted_values) - 1) * fraction))
    return sorted_values[index]


def summarize(samples_ns: list[int]) -> dict[str, float | int]:
    values_us = sorted(value / 1_000 for value in samples_ns)
    return {
        "samples": len(values_us),
        "mean_us": round(statistics.fmean(values_us), 3),
        "stdev_us": (
            round(statistics.stdev(values_us), 3) if len(values_us) > 1 else 0.0
        ),
        "p50_us": round(percentile(values_us, 0.50), 3),
        "p90_us": round(percentile(values_us, 0.90), 3),
        "p95_us": round(percentile(values_us, 0.95), 3),
        "p99_us": round(percentile(values_us, 0.99), 3),
        "min_us": round(values_us[0], 3),
        "max_us": round(values_us[-1], 3),
    }


def measure(
    operation: Callable[[], Any], iterations: int, warmup: int
) -> dict[str, Any]:
    for _ in range(warmup):
        operation()
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        operation()
        samples.append(time.perf_counter_ns() - started)
    return summarize(samples)


class _SuccessfulResponse:
    def raise_for_status(self) -> None:
        return None


class _LocalHTTPClient:
    def post(self, *args: Any, **kwargs: Any) -> _SuccessfulResponse:
        return _SuccessfulResponse()

    def close(self) -> None:
        return None


def benchmark_sdk(iterations: int, warmup: int) -> dict[str, Any]:
    tracker = LLMWitnessTracker(max_queue_size=iterations + warmup + 10)
    tracker.http_client.close()
    tracker.http_client = _LocalHTTPClient()  # type: ignore[assignment]

    def record() -> None:
        tracker.record_event(
            task_name="benchmark",
            completion_string="Example result",
            agent_state={"step": 1},
        )

    result = measure(record, iterations, warmup)
    tracker.flush()
    tracker.shutdown()
    result["dropped_events"] = tracker.dropped_events
    result["delivery_failures"] = tracker.delivery_failures
    return result


def passport_reference_case() -> dict[str, Any]:
    """Create one isolated, in-process Passport case for local timing only."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    manager = Ed25519KeyManager()
    issuer = PassportIssuer("benchmark-issuer", "benchmark-key", manager)
    invocation = PassportInvocation(
        principal_id="benchmark-principal",
        agent_id="benchmark-agent",
        audience="benchmark-service",
        environment="local",
        effect_name="reference.refund",
        resource="benchmark-resource",
        value=1,
        risk_tier=RiskTier.R1,
        nonce="0123456789abcdef",
        request_hash=sha256_ref("passport-benchmark-request"),
    )
    claims = PassportClaims(
        principal_id=invocation.principal_id,
        agent_id=invocation.agent_id,
        audience=invocation.audience,
        environment=invocation.environment,
        policy_version="benchmark-v1",
        capability=PassportCapability(
            effects=("reference.refund",),
            resources=("benchmark-resource",),
            maximum_value=1,
            maximum_risk=RiskTier.R1,
        ),
        invocation_hash=invocation.digest(),
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
    )
    credential = issuer.issue(claims)
    anchors = {
        ("benchmark-issuer", "benchmark-key"): PassportTrustAnchor(
            issuer_id="benchmark-issuer",
            key_id="benchmark-key",
            public_key_pem=manager.export_public_key_pem(),
        )
    }
    verifier = PassportVerifier(replay_guard=InMemoryNonceReplayGuard())

    def status(_credential: Any) -> PassportStatus:
        return PassportStatus(active=True, checked_at=now)

    verified = verifier.verify([credential], invocation, anchors, status, now=now)
    policy = AuthorityPolicy(
        "benchmark-policy",
        frozenset({"reference.refund"}),
        frozenset({"benchmark-principal"}),
        maximum_risk=RiskTier.R1,
    )
    return {
        "issuer": issuer,
        "claims": claims,
        "verifier": verifier,
        "credential": credential,
        "invocation": invocation,
        "anchors": anchors,
        "status": status,
        "now": now,
        "verification": verified,
        "policy": policy,
    }


async def benchmark_gateway(iterations: int, warmup: int) -> dict[str, Any]:
    previous_mock = os.environ.get("LLMWITNESS_MOCK_UPSTREAM")
    os.environ["LLMWITNESS_MOCK_UPSTREAM"] = "true"
    old_client = gateway.http_client
    gateway.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json={"status": "accepted"})
        )
    )
    transport = httpx.ASGITransport(app=gateway.app)
    samples: list[int] = []
    payload = {
        "model": "benchmark-model",
        "messages": [{"role": "user", "content": "hello"}],
    }
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            for index in range(iterations + warmup):
                started = time.perf_counter_ns()
                response = await client.post("/v1/chat/completions", json=payload)
                response.raise_for_status()
                elapsed = time.perf_counter_ns() - started
                if index >= warmup:
                    samples.append(elapsed)
    finally:
        await gateway.http_client.aclose()
        gateway.http_client = old_client
        if previous_mock is None:
            os.environ.pop("LLMWITNESS_MOCK_UPSTREAM", None)
        else:
            os.environ["LLMWITNESS_MOCK_UPSTREAM"] = previous_mock
    return summarize(samples)


def git_commit() -> str | None:
    git_executable = shutil.which("git")
    if git_executable is None:
        return None
    try:
        return subprocess.check_output(  # noqa: S603 - executable resolved with shutil.which
            [git_executable, "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_is_dirty() -> bool | None:
    git_executable = shutil.which("git")
    if git_executable is None:
        return None
    try:
        output = subprocess.check_output(  # noqa: S603 - executable resolved with shutil.which
            [git_executable, "status", "--porcelain"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return bool(output.strip())
    except (OSError, subprocess.CalledProcessError):
        return None


def dependency_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for package in ("fastapi", "httpx", "pydantic", "cryptography"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = None
    return result


def run(iterations: int, warmup: int) -> dict[str, Any]:
    payload = {
        "customer": {"ssn": "123-45-6789", "card": "4111 1111 1111 1111"},
        "authorization": "Bearer abcdefghijklmnopqrstuvwxyz",
        "messages": [{"content": "Example telemetry content"}],
    }
    key_manager = Ed25519KeyManager()
    signed_payload = canonical_json(
        {"correlation_id": "benchmark", "events": [payload]}
    )
    signature = key_manager.sign(signed_payload)
    passport = passport_reference_case()

    return {
        "schema_version": 1,
        "disclaimer": "Machine-specific development measurements; not an SLA or universal claim.",
        "environment": {
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "git_commit": git_commit(),
            "git_worktree_dirty": git_is_dirty(),
            "python": sys.version,
            "platform": platform.platform(),
            "processor": platform.processor() or None,
            "logical_cpu_count": os.cpu_count(),
            "dependencies": dependency_versions(),
        },
        "methodology": {
            "clock": "time.perf_counter_ns",
            "percentile_method": "nearest-rank observation from sorted samples",
            "standard_deviation": "sample standard deviation",
            "iterations_per_case": iterations,
            "warmup_iterations_per_case": warmup,
            "process_model": "single process",
        },
        "cases": {
            "redact_payload": {
                **measure(lambda: redact_payload(payload), iterations, warmup),
                "input_bytes": len(canonical_json(payload).encode("utf-8")),
            },
            "ed25519_sign": measure(
                lambda: key_manager.sign(signed_payload), iterations, warmup
            ),
            "ed25519_verify": measure(
                lambda: key_manager.verify(signed_payload, signature),
                iterations,
                warmup,
            ),
            "passport_issue": measure(
                lambda: passport["issuer"].issue(passport["claims"]),
                iterations,
                warmup,
            ),
            "passport_verify": measure(
                lambda: PassportVerifier(
                    replay_guard=InMemoryNonceReplayGuard()
                ).verify(
                    [passport["credential"]],
                    passport["invocation"],
                    passport["anchors"],
                    passport["status"],
                    now=passport["now"],
                ),
                iterations,
                warmup,
            ),
            "passport_runtime_map": measure(
                lambda: PassportAuthorityMapper().evaluate(
                    passport["verification"],
                    passport["policy"],
                    passport["invocation"],
                ),
                iterations,
                warmup,
            ),
            "sdk_record_event": benchmark_sdk(iterations, warmup),
            "mock_gateway_request": asyncio.run(benchmark_gateway(iterations, warmup)),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations <= 0 or args.warmup < 0:
        parser.error("iterations must be positive and warmup must be non-negative")
    result = run(args.iterations, args.warmup)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
