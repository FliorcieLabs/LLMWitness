"""LLMWitness Community command-line interface.

This module is an edge-only composition boundary: it parses local arguments
and delegates behavior to the owning phase module.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from llmwitness.config import get_config
from llmwitness.utils import verify_proof_receipt

if TYPE_CHECKING:
    from llmwitness.reliability import ScenarioResult

CommandHandler = Callable[[argparse.Namespace], None]


def _add_receipt_commands(subparsers: argparse._SubParsersAction) -> None:
    subparsers.add_parser(
        "validate-config", help="Validate local environment configuration"
    )
    verify_parser = subparsers.add_parser(
        "verify", help="Verify a tamper-evident local receipt"
    )
    verify_parser.add_argument("receipt", help="Path to a receipt JSON file")
    verify_parser.add_argument(
        "--secret-key",
        default=None,
        help="Also verify the optional HMAC with this secret (prefer the environment variable)",
    )
    verify_parser.add_argument(
        "--trusted-fingerprint",
        default=None,
        help="Require the receipt's Ed25519 public-key fingerprint to match this value "
        "(default: LLMWITNESS_TRUSTED_FINGERPRINT, then the local key from `llmwitness keygen`)",
    )
    verify_parser.add_argument(
        "--any-signer",
        action="store_true",
        help="Skip the signer check even when a trusted fingerprint is configured",
    )


def _add_local_tool_commands(subparsers: argparse._SubParsersAction) -> None:
    keygen = subparsers.add_parser(
        "keygen", help="Create a persistent local signing key and print its fingerprint"
    )
    keygen.add_argument("--key-dir", default=None)

    serve = subparsers.add_parser(
        "serve", help="Run the ingestion service and the gateway in one process"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--ingest-port", type=int, default=8000)
    serve.add_argument("--gateway-port", type=int, default=8011)
    serve.add_argument("--no-gateway", action="store_true")

    seal = subparsers.add_parser(
        "seal", help="Seal one run into a signed local receipt"
    )
    seal.add_argument("correlation_id")
    seal.add_argument("--ingestion-url", default=None)

    listing = subparsers.add_parser("list", help="List local receipts, newest first")
    listing.add_argument("--receipt-dir", default=None)
    listing.add_argument("--json", action="store_true")

    show = subparsers.add_parser("show", help="Show the timeline of one receipt")
    show.add_argument("receipt", help="Receipt path or correlation ID")
    show.add_argument("--receipt-dir", default=None)
    show.add_argument("--html", default=None, help="Write a static HTML page instead")

    diff = subparsers.add_parser("diff", help="Show where two runs diverged")
    diff.add_argument("first", help="Receipt path or correlation ID")
    diff.add_argument("second", help="Receipt path or correlation ID")
    diff.add_argument("--receipt-dir", default=None)

    chain = subparsers.add_parser(
        "verify-chain", help="Check the hash chain across a receipt directory"
    )
    chain.add_argument("--receipt-dir", default=None)

    otel = subparsers.add_parser(
        "export-otel", help="Export one receipt as OpenTelemetry trace data (OTLP/JSON)"
    )
    otel.add_argument("receipt", help="Receipt path or correlation ID")
    otel.add_argument("--receipt-dir", default=None)
    otel.add_argument("--output", default=None, help="Write OTLP JSON to this file")
    otel.add_argument(
        "--endpoint", default=None, help="POST to this OTLP/HTTP collector"
    )
    otel.add_argument("--service-name", default="llmwitness")

    stamp = subparsers.add_parser(
        "timestamp", help="Request an RFC 3161 timestamp token for a receipt"
    )
    stamp.add_argument("receipt", help="Receipt path or correlation ID")
    stamp.add_argument("--receipt-dir", default=None)
    stamp.add_argument("--tsa-url", required=True, help="Time-stamping authority URL")


def _add_envelope_commands(subparsers: argparse._SubParsersAction) -> None:
    schema_parser = subparsers.add_parser(
        "schema", help="Print the Execution Envelope v0.1 JSON Schema"
    )
    schema_parser.add_argument("--compact", action="store_true")
    envelope_parser = subparsers.add_parser(
        "validate-envelope", help="Validate an Execution Envelope JSON file"
    )
    envelope_parser.add_argument("envelope")


def _add_journal_commands(subparsers: argparse._SubParsersAction) -> None:
    verify_parser = subparsers.add_parser(
        "verify-journal", help="Verify one run in a local hash-linked journal"
    )
    verify_parser.add_argument("journal")
    verify_parser.add_argument("run_id")
    show_parser = subparsers.add_parser(
        "show-run", help="Print ordered entries for one local journal run"
    )
    show_parser.add_argument("journal")
    show_parser.add_argument("run_id")


def _add_evidence_bundle_command(subparsers: argparse._SubParsersAction) -> None:
    bundle_parser = subparsers.add_parser(
        "evidence-bundle", help="Export or verify a portable local evidence bundle"
    )
    bundle_commands = bundle_parser.add_subparsers(dest="bundle_command", required=True)
    bundle_export = bundle_commands.add_parser(
        "export", help="Export one verified journal run without artifact bytes"
    )
    bundle_export.add_argument("--journal", default=".llmwitness/journal.db")
    bundle_export.add_argument("--run-id", required=True)
    bundle_export.add_argument("--output", required=True)
    bundle_verify = bundle_commands.add_parser(
        "verify", help="Verify a bundle offline without extracting it"
    )
    bundle_verify.add_argument("bundle")


def _add_reliability_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "reliability", help="Run deterministic Community reliability scenarios"
    )
    parser.add_argument("--output-dir", default=".llmwitness/reliability")
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument(
        "--baseline", help="Compare against a reviewed reliability baseline"
    )
    baseline.add_argument(
        "--write-baseline",
        help="Explicitly write a new baseline when every scenario passes",
    )


def _add_replay_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "replay", help="Replay a run without live external writes by default"
    )
    parser.add_argument("run_id")
    parser.add_argument("--journal", default=".llmwitness/journal.db")
    parser.add_argument(
        "--mode",
        choices=["exact", "simulated", "counterfactual", "live"],
        default="simulated",
    )
    parser.add_argument("--deterministic-boundary", action="store_true")
    parser.add_argument("--allow-side-effects", action="store_true")
    parser.add_argument("--allow-effect", action="append", default=[])
    parser.add_argument("--set", action="append", default=[])


def _add_project_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "project", help="Create, validate, or run local reference projects"
    )
    commands = parser.add_subparsers(dest="project_command", required=True)
    project_init = commands.add_parser(
        "init", help="Create a deterministic two-job reference project"
    )
    project_init.add_argument("directory")
    project_validate = commands.add_parser(
        "validate", help="Validate a project without creating runtime state"
    )
    project_validate.add_argument("config")
    project_plan = commands.add_parser(
        "plan", help="Explain authority and contract gates without runtime state"
    )
    project_plan.add_argument("config")
    selection = project_plan.add_mutually_exclusive_group(required=True)
    selection.add_argument("--job")
    selection.add_argument("--all", action="store_true")
    project_run = commands.add_parser("run", help="Run one or all local reference jobs")
    project_run.add_argument("config")
    selection = project_run.add_mutually_exclusive_group(required=True)
    selection.add_argument("--job")
    selection.add_argument("--all", action="store_true")
    commands.add_parser("schema", help="Print the Fliorcie project v0.1 JSON Schema")


def _add_recovery_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "recover", help="Inspect the read-only local recovery inbox"
    )
    commands = parser.add_subparsers(dest="recovery_command", required=True)
    recovery_list = commands.add_parser(
        "list", help="List effects that require reconciliation or review"
    )
    recovery_list.add_argument("--journal", default=".llmwitness/journal.db")
    recovery_list.add_argument("--include-terminal", action="store_true")
    recovery_inspect = commands.add_parser(
        "inspect", help="Verify and inspect one recovery run"
    )
    recovery_inspect.add_argument("--journal", default=".llmwitness/journal.db")
    recovery_inspect.add_argument("--run-id", required=True)


def _add_provider_dossier_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "provider-dossier",
        help="Validate provider metadata for human adapter review",
    )
    parser.add_argument("dossier", help="Path to a provider-dossier JSON file")
    parser.add_argument("--output-dir", default=".llmwitness/provider-readiness")


def _add_bench_intake_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "bench-intake",
        help="Validate Bench governance metadata for human acceptance",
    )
    parser.add_argument("manifest", help="Path to a Bench intake-manifest JSON file")
    parser.add_argument("--output-dir", default=".llmwitness/bench-intake")


def _add_release_readiness_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "release-readiness",
        help="Validate release-control metadata for maintainer review",
    )
    parser.add_argument("manifest", help="Path to a release-readiness JSON file")
    parser.add_argument("--output-dir", default=".llmwitness/release-readiness")


def _add_pilot_evidence_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "pilot-evidence",
        help="Validate external-pilot metadata for independent review",
    )
    parser.add_argument("manifest", help="Path to an external-pilot manifest")
    parser.add_argument("--output-dir", default=".llmwitness/pilot-evidence")


def _add_naming_readiness_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "naming-readiness",
        help="Validate naming-clearance metadata for founder/legal review",
    )
    parser.add_argument("dossier", help="Path to a naming-clearance dossier")
    parser.add_argument("--output-dir", default=".llmwitness/naming-readiness")


def _add_adapter_conformance_command(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "adapter-conformance",
        help="Run deterministic framework mapping-adapter conformance",
    )
    parser.add_argument("--output-dir", default=".llmwitness/adapter-conformance")


def build_parser() -> argparse.ArgumentParser:
    """Build the stable local-only LLMWitness command surface."""
    parser = argparse.ArgumentParser(
        prog="llmwitness", description="Inspect and verify local LLMWitness receipts"
    )
    subparsers = parser.add_subparsers(dest="command")
    _add_receipt_commands(subparsers)
    _add_local_tool_commands(subparsers)
    _add_envelope_commands(subparsers)
    _add_journal_commands(subparsers)
    _add_evidence_bundle_command(subparsers)
    _add_reliability_command(subparsers)
    _add_replay_command(subparsers)
    _add_project_commands(subparsers)
    _add_recovery_commands(subparsers)
    _add_provider_dossier_command(subparsers)
    _add_bench_intake_command(subparsers)
    _add_release_readiness_command(subparsers)
    _add_pilot_evidence_command(subparsers)
    _add_naming_readiness_command(subparsers)
    _add_adapter_conformance_command(subparsers)
    subparsers.add_parser("mcp", help="Start the local STDIO MCP server")
    return parser


def _handle_validate_config(_: argparse.Namespace) -> None:
    errors = get_config().validate()
    if errors:
        for error in errors:
            print(f"[ERROR] {error}")
        raise SystemExit(1)
    print("[OK] Local LLMWitness configuration is valid.")


def _handle_verify_receipt(args: argparse.Namespace) -> None:
    secret = args.secret_key or os.getenv("LLMWITNESS_SECRET_KEY")
    if not os.path.isfile(args.receipt):
        print(f"[ERROR] Receipt file not found: {args.receipt}")
        raise SystemExit(1)
    if not verify_proof_receipt(args.receipt, secret):
        print("[FAIL] Receipt signature verification failed.")
        raise SystemExit(1)
    try:
        with open(args.receipt, encoding="utf-8") as handle:
            fingerprint = json.load(handle).get("public_key_fingerprint")
    except (OSError, ValueError, AttributeError):
        print("[FAIL] Receipt fingerprint could not be read.")
        raise SystemExit(1) from None
    if not isinstance(fingerprint, str) or not fingerprint:
        print("[FAIL] Receipt does not contain a public-key fingerprint.")
        raise SystemExit(1)
    trusted, trust_source = _trusted_fingerprint(args)
    if trusted is not None and fingerprint.lower() != trusted.lower():
        print("[FAIL] Receipt signer fingerprint does not match the trusted value.")
        print(f"Trusted fingerprint ({trust_source}): {trusted}")
        print(f"Receipt signer fingerprint: {fingerprint}")
        raise SystemExit(1)
    if secret:
        print("[OK] Ed25519 signature and HMAC verified.")
    elif trusted is not None:
        print(f"[OK] Ed25519 signature verified; signer matches {trust_source}.")
    else:
        print(
            "[OK] Ed25519 signature verified. Trust the signer only after checking its fingerprint."
        )
    print(f"Signer fingerprint: {fingerprint}")


def _trusted_fingerprint(args: argparse.Namespace) -> tuple[str | None, str]:
    """Pick the fingerprint a receipt's signer must match, and say where it came from."""
    if args.trusted_fingerprint is not None:
        return args.trusted_fingerprint, "--trusted-fingerprint"
    if getattr(args, "any_signer", False):
        return None, ""
    from_env = os.getenv("LLMWITNESS_TRUSTED_FINGERPRINT")
    if from_env:
        return from_env, "LLMWITNESS_TRUSTED_FINGERPRINT"
    from llmwitness.keys import local_trusted_fingerprint

    local = local_trusted_fingerprint()
    return (local, "the local signing key") if local else (None, "")


def _handle_keygen(args: argparse.Namespace) -> None:
    from llmwitness.keys import create_identity

    try:
        identity = create_identity(args.key_dir)
    except FileExistsError as exc:
        print(f"[ERROR] {exc}. Existing keys are never overwritten.")
        raise SystemExit(1) from None
    print(f"[OK] Created local signing key in {identity.directory}")
    print(f"Signer fingerprint: {identity.fingerprint}")
    print(
        "Keep the private key file private: anyone who can read it can sign receipts."
    )


def _handle_serve(args: argparse.Namespace) -> None:
    import asyncio

    import uvicorn

    from llmwitness import gateway, ingest

    servers = [
        uvicorn.Server(
            uvicorn.Config(ingest.app, host=args.host, port=args.ingest_port)
        )
    ]
    if not args.no_gateway:
        if not os.getenv("INGESTION_SERVER_URL"):
            gateway.INGESTION_SERVER_URL = f"http://{args.host}:{args.ingest_port}"
        servers.append(
            uvicorn.Server(
                uvicorn.Config(gateway.app, host=args.host, port=args.gateway_port)
            )
        )

    async def run() -> None:
        tasks = [asyncio.ensure_future(server.serve()) for server in servers]
        # When either service stops (Ctrl-C or a bind failure), stop the other.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def _handle_seal(args: argparse.Namespace) -> None:
    import httpx

    base = (
        args.ingestion_url
        or os.getenv("INGESTION_SERVER_URL")
        or "http://localhost:8000"
    ).rstrip("/")
    token = os.getenv("LLMWITNESS_INGEST_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = httpx.post(
            f"{base}/ingest/seal",
            json={"correlation_id": args.correlation_id},
            headers=headers,
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        print(f"[ERROR] Could not reach the ingestion service at {base}: {exc}")
        raise SystemExit(1) from None
    if response.status_code != 200:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        print(f"[FAIL] Seal refused (HTTP {response.status_code}): {detail}")
        raise SystemExit(1)
    result = response.json()
    print(f"[OK] Receipt created: {result.get('receipt_file')}")
    print(f"Signer fingerprint: {result.get('public_key_fingerprint')}")


def _resolve_or_exit(reference: str, directory: str | None) -> Path:
    from llmwitness.receipt_tools import resolve_receipt

    try:
        return resolve_receipt(reference, directory)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1) from None


def _load_or_exit(path: Path) -> dict:
    from llmwitness.receipt_tools import load_receipt

    try:
        return load_receipt(path)
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {path} is not a readable receipt: {exc}")
        raise SystemExit(1) from None


def _handle_list(args: argparse.Namespace) -> None:
    from llmwitness.receipt_tools import list_receipts, receipt_directory

    summaries = list_receipts(args.receipt_dir)
    if args.json:
        print(json.dumps([item.to_dict() for item in summaries], indent=2))
        return
    if not summaries:
        print(f"No receipts in {receipt_directory(args.receipt_dir)}")
        return
    for item in summaries:
        events = item.sdk_events + item.gateway_events + item.extension_events
        print(
            f"{item.correlation_id}  {item.sealed_at[:19]}  {events:>4} events  "
            f"{item.prompt_tokens + item.completion_tokens:>7} tokens  "
            f"{'valid' if item.signature_valid else 'INVALID'}"
        )


def _handle_show(args: argparse.Namespace) -> None:
    from llmwitness.receipt_tools import render_html, render_text

    path = _resolve_or_exit(args.receipt, args.receipt_dir)
    receipt = _load_or_exit(path)
    if args.html:
        Path(args.html).write_text(render_html(path, receipt), encoding="utf-8")
        print(f"[OK] Wrote {args.html}")
        return
    print(render_text(path, receipt), end="")


def _handle_diff(args: argparse.Namespace) -> None:
    from llmwitness.receipt_tools import diff_receipts

    first = _load_or_exit(_resolve_or_exit(args.first, args.receipt_dir))
    second = _load_or_exit(_resolve_or_exit(args.second, args.receipt_dir))
    differences = diff_receipts(first, second)
    if not differences:
        print("No differences in events, tokens, tools or outputs.")
        return
    print("\n".join(differences))
    raise SystemExit(1)


def _handle_verify_chain(args: argparse.Namespace) -> None:
    from llmwitness.receipt_tools import receipt_directory, verify_chain

    report = verify_chain(args.receipt_dir)
    print(
        f"{report.chained} chained receipt(s), {report.unchained} older unchained "
        f"receipt(s) in {receipt_directory(args.receipt_dir)}"
    )
    if len(report.signers) > 1:
        print(f"[WARN] The chain was signed by {len(report.signers)} different keys.")
    if not report.valid:
        for problem in report.problems:
            print(f"[FAIL] {problem}")
        raise SystemExit(1)
    print("[OK] Receipt chain is unbroken.")


def _handle_export_otel(args: argparse.Namespace) -> None:
    import httpx

    from llmwitness.otel_export import post_otlp, receipt_to_otlp

    receipt = _load_or_exit(_resolve_or_exit(args.receipt, args.receipt_dir))
    try:
        payload = receipt_to_otlp(receipt, service_name=args.service_name)
    except ValueError as exc:
        print(f"[ERROR] Receipt cannot be exported: {exc}")
        raise SystemExit(1) from None
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"[OK] Wrote {args.output}")
    if args.endpoint:
        try:
            post_otlp(args.endpoint, payload)
        except httpx.HTTPError as exc:
            print(f"[ERROR] Collector did not accept the trace: {exc}")
            raise SystemExit(1) from None
        print(f"[OK] Sent trace to {args.endpoint}")
    if not args.output and not args.endpoint:
        print(json.dumps(payload, indent=2))


def _handle_timestamp(args: argparse.Namespace) -> None:
    import httpx

    from llmwitness.timestamping import request_timestamp

    path = _resolve_or_exit(args.receipt, args.receipt_dir)
    try:
        token_path, digest = request_timestamp(path, args.tsa_url)
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"[ERROR] Timestamp request failed: {exc}")
        raise SystemExit(1) from None
    print(f"[OK] Timestamp token saved: {token_path}")
    print(f"Stamped SHA-256 digest: {digest}")
    print(
        "The token's signature was not checked here. Verify it with:\n"
        f"  openssl ts -verify -digest {digest} -in {token_path} -CAfile <tsa-ca.pem>"
    )


def _handle_schema(args: argparse.Namespace) -> None:
    from llmwitness.envelope import ExecutionEnvelope

    print(
        json.dumps(
            ExecutionEnvelope.model_json_schema(), indent=None if args.compact else 2
        )
    )


def _handle_validate_envelope(args: argparse.Namespace) -> None:
    from pydantic import ValidationError

    from llmwitness.envelope import read_envelope

    try:
        envelope = read_envelope(
            json.loads(Path(args.envelope).read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, ValidationError) as exc:
        print(f"[FAIL] {exc}")
        raise SystemExit(1) from None
    print(f"[OK] Execution Envelope {envelope.schema_version}: {envelope.event_id}")


def _handle_journal(args: argparse.Namespace) -> None:
    from llmwitness.journal import SQLiteJournalStore

    if not os.path.isfile(args.journal):
        print(f"[ERROR] Journal file not found: {args.journal}")
        raise SystemExit(1)
    with SQLiteJournalStore(args.journal, read_only=True) as journal:
        if args.command == "verify-journal":
            result = journal.verify(args.run_id)
            print(json.dumps(result.__dict__, indent=2))
            if not result.valid:
                raise SystemExit(1)
            return
        print(
            json.dumps(
                [entry.__dict__ for entry in journal.scan(args.run_id)], indent=2
            )
        )


def _handle_evidence_bundle(args: argparse.Namespace) -> None:
    from llmwitness.evidence_bundle import (
        export_evidence_bundle,
        verify_evidence_bundle,
    )

    if args.bundle_command == "verify":
        result = verify_evidence_bundle(args.bundle)
        print(json.dumps(result.__dict__, indent=2))
        if not result.valid:
            raise SystemExit(1)
        return
    from llmwitness.journal import SQLiteJournalStore

    if not os.path.isfile(args.journal):
        print(f"[ERROR] Journal file not found: {args.journal}")
        raise SystemExit(1)
    try:
        with SQLiteJournalStore(args.journal, read_only=True) as journal:
            manifest = export_evidence_bundle(journal, args.run_id, args.output)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
    print(json.dumps(manifest.model_dump(mode="json"), indent=2))


def _handle_reliability(args: argparse.Namespace) -> None:
    from llmwitness.reliability import run_reliability_corpus, write_reports

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    results = run_reliability_corpus()
    write_reports(
        results,
        json_path=output / "reliability.json",
        junit_path=output / "reliability.junit.xml",
        markdown_path=output / "reliability.md",
    )
    failed = sum(not result.passed for result in results)
    print(
        f"[{'FAIL' if failed else 'OK'}] {len(results) - failed}/{len(results)} reliability scenarios passed"
    )
    baseline_failed = _handle_reliability_baseline(args, results, failed, output)
    if failed or baseline_failed:
        raise SystemExit(1)


def _handle_reliability_baseline(
    args: argparse.Namespace,
    results: list[ScenarioResult],
    failed: int,
    output: Path,
) -> bool:
    from llmwitness.reliability import (
        compare_reliability_baseline,
        create_reliability_baseline,
        load_reliability_baseline,
        write_reliability_baseline,
        write_reliability_comparison,
    )

    if args.write_baseline:
        if failed:
            print("[FAIL] A baseline can be written only from an all-passing run.")
            return True
        baseline = create_reliability_baseline(results)
        write_reliability_baseline(baseline, Path(args.write_baseline))
        print(
            f"[OK] Wrote {baseline.sample_count}-scenario baseline: {args.write_baseline}"
        )
        return False
    if not args.baseline:
        return False
    try:
        baseline = load_reliability_baseline(Path(args.baseline))
        comparison = compare_reliability_baseline(baseline, results)
        write_reliability_comparison(
            comparison,
            json_path=output / "reliability.comparison.json",
            markdown_path=output / "reliability.comparison.md",
        )
    except (OSError, ValueError) as exc:
        print(f"[FAIL] Baseline comparison could not run: {exc}")
        raise SystemExit(1) from None
    print(
        f"[{'OK' if comparison.policy_passed else 'FAIL'}] Reliability baseline policy "
        f"{'passed' if comparison.policy_passed else 'failed'}"
    )
    return not comparison.policy_passed


def _handle_replay(args: argparse.Namespace) -> None:
    from llmwitness.journal import SQLiteJournalStore
    from llmwitness.replay import ReplayEngine, ReplayMode

    changes: dict[str, str] = {}
    for item in args.set:
        if "=" not in item:
            print(f"[ERROR] --set must use key=value: {item}")
            raise SystemExit(1)
        key, value = item.split("=", 1)
        changes[key] = value
    with SQLiteJournalStore(args.journal, read_only=True) as journal:
        try:
            replayed = ReplayEngine().replay(
                journal.scan(args.run_id),
                ReplayMode(args.mode),
                deterministic_boundary=args.deterministic_boundary,
                changes=changes,
                allow_side_effects=args.allow_side_effects,
                effect_allowlist=frozenset(args.allow_effect),
            )
        except (ValueError, PermissionError, NotImplementedError) as exc:
            print(f"[FAIL] {exc}")
            raise SystemExit(1) from None
    print(json.dumps(replayed, indent=2))


def _handle_project(args: argparse.Namespace) -> None:
    import asyncio

    from pydantic import ValidationError

    from llmwitness.projects import (
        ProjectConfig,
        ProjectRunner,
        bootstrap_project,
        load_project,
    )

    if args.project_command == "schema":
        print(json.dumps(ProjectConfig.model_json_schema(), indent=2))
        return
    try:
        if args.project_command == "init":
            print(
                f"[OK] Created local reference project: {bootstrap_project(args.directory)}"
            )
            return
        project = load_project(args.config)
        if args.project_command == "validate":
            jobs = ", ".join(job.job_id for job in project.config.jobs)
            print(f"[OK] Project {project.config.project_id!r} is valid; jobs: {jobs}")
            return
        runner = ProjectRunner(project)
        if args.project_command == "plan":
            plans = (
                asyncio.run(runner.plan_all())
                if args.all
                else (asyncio.run(runner.plan(args.job)),)
            )
            print(
                json.dumps([item.model_dump(mode="json") for item in plans], indent=2)
            )
            return
        result = (
            asyncio.run(runner.run_all())
            if args.all
            else asyncio.run(runner.run(args.job))
        )
    except (OSError, ValueError, KeyError, ValidationError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    if result.outcome != "completed":
        raise SystemExit(1)


def _handle_recovery(args: argparse.Namespace) -> None:
    from llmwitness.journal import SQLiteJournalStore
    from llmwitness.recovery import RecoveryPlanner

    if not os.path.isfile(args.journal):
        print(f"[ERROR] Journal file not found: {args.journal}")
        raise SystemExit(1)
    with SQLiteJournalStore(args.journal, read_only=True) as journal:
        if args.recovery_command == "list":
            try:
                items = RecoveryPlanner(journal).list_items(
                    include_terminal=args.include_terminal
                )
            except Exception as exc:
                print(f"[FAIL] {type(exc).__name__}: {exc}")
                raise SystemExit(1) from None
            print(
                json.dumps([item.model_dump(mode="json") for item in items], indent=2)
            )
            return
        verification = journal.verify(args.run_id)
        if not verification.valid:
            print(f"[FAIL] {verification.error}")
            raise SystemExit(1)
        print(
            json.dumps(
                {
                    "verification": verification.__dict__,
                    "entries": [entry.__dict__ for entry in journal.scan(args.run_id)],
                },
                indent=2,
            )
        )


def _handle_provider_dossier(args: argparse.Namespace) -> None:
    from llmwitness.provider_readiness import (
        load_provider_dossier,
        validate_provider_dossier,
        write_provider_readiness_reports,
    )

    try:
        payload = load_provider_dossier(args.dossier)
        report = validate_provider_dossier(payload)
        json_path, markdown_path = write_provider_readiness_reports(
            report, args.output_dir
        )
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
    status = "OK" if report.ready_for_human_review else "FAIL"
    print(f"[{status}] Provider dossier report: {json_path}")
    print(f"[{status}] Human-readable report: {markdown_path}")
    if not report.ready_for_human_review:
        raise SystemExit(1)


def _handle_bench_intake(args: argparse.Namespace) -> None:
    from llmwitness.bench_governance import (
        load_bench_dataset_intake,
        validate_bench_dataset_intake,
        write_bench_dataset_intake_reports,
    )

    try:
        payload = load_bench_dataset_intake(args.manifest)
        report = validate_bench_dataset_intake(payload)
        json_path, markdown_path = write_bench_dataset_intake_reports(
            report, args.output_dir
        )
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
    status = "OK" if report.ready_for_human_acceptance else "FAIL"
    print(f"[{status}] Bench intake report: {json_path}")
    print(f"[{status}] Human-readable report: {markdown_path}")
    if report.pending_review_roles:
        print("[PENDING] Human acceptance is still required.")
    if not report.ready_for_human_acceptance:
        raise SystemExit(1)


def _handle_release_readiness(args: argparse.Namespace) -> None:
    from llmwitness.release_readiness import (
        load_release_readiness_manifest,
        validate_release_readiness,
        write_release_readiness_reports,
    )

    try:
        payload = load_release_readiness_manifest(args.manifest)
        report = validate_release_readiness(payload)
        json_path, markdown_path = write_release_readiness_reports(
            report, args.output_dir
        )
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
    status = "OK" if report.ready_for_maintainer_decision else "FAIL"
    print(f"[{status}] Release-readiness report: {json_path}")
    print(f"[{status}] Human-readable report: {markdown_path}")
    if report.pending_control_roles:
        print("[PENDING] Explicit maintainer ship/no-ship decision is required.")
    print("[INFO] This command never authorizes or performs publication.")
    if not report.ready_for_maintainer_decision:
        raise SystemExit(1)


def _handle_pilot_evidence(args: argparse.Namespace) -> None:
    from llmwitness.pilot_evidence import (
        load_external_pilot_evidence,
        validate_external_pilot_evidence,
        write_pilot_evidence_reports,
    )

    try:
        payload = load_external_pilot_evidence(args.manifest)
        report = validate_external_pilot_evidence(payload)
        json_path, markdown_path = write_pilot_evidence_reports(report, args.output_dir)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
    status = "OK" if report.ready_for_independent_review else "FAIL"
    print(f"[{status}] External-pilot evidence report: {json_path}")
    print(f"[{status}] Human-readable report: {markdown_path}")
    if report.pending_review_roles:
        print("[PENDING] Independent human review is still required.")
    print("[INFO] This command never establishes external adoption.")
    if not report.ready_for_independent_review:
        raise SystemExit(1)


def _handle_naming_readiness(args: argparse.Namespace) -> None:
    from llmwitness.naming_readiness import (
        load_naming_clearance_dossier,
        validate_naming_clearance_dossier,
        write_naming_readiness_reports,
    )

    try:
        payload = load_naming_clearance_dossier(args.dossier)
        report = validate_naming_clearance_dossier(payload)
        json_path, markdown_path = write_naming_readiness_reports(
            report, args.output_dir
        )
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
    status = "OK" if report.ready_for_founder_legal_review else "FAIL"
    print(f"[{status}] Naming-readiness report: {json_path}")
    print(f"[{status}] Human-readable report: {markdown_path}")
    if report.pending_review_roles:
        print("[PENDING] Founder/legal decisions are still required.")
    print("[INFO] This command never establishes legal clearance.")
    if not report.ready_for_founder_legal_review:
        raise SystemExit(1)


def _handle_adapter_conformance(args: argparse.Namespace) -> None:
    from llmwitness.adapter_conformance import (
        run_adapter_conformance,
        write_adapter_conformance_reports,
    )

    report = run_adapter_conformance()
    json_path, markdown_path = write_adapter_conformance_reports(
        report, args.output_dir
    )
    status = "OK" if report.failed == 0 else "FAIL"
    print(
        f"[{status}] {report.passed}/{report.sample_count} mapping adapters passed: {json_path}"
    )
    print(f"[{status}] Human-readable report: {markdown_path}")
    print("[INFO] This result does not certify a real framework SDK or native hook.")
    if report.failed:
        raise SystemExit(1)


def _handle_mcp(_: argparse.Namespace) -> None:
    from llmwitness.mcp_server import run_stdio

    run_stdio()


COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "validate-config": _handle_validate_config,
    "verify": _handle_verify_receipt,
    "keygen": _handle_keygen,
    "serve": _handle_serve,
    "seal": _handle_seal,
    "list": _handle_list,
    "show": _handle_show,
    "diff": _handle_diff,
    "verify-chain": _handle_verify_chain,
    "export-otel": _handle_export_otel,
    "timestamp": _handle_timestamp,
    "schema": _handle_schema,
    "validate-envelope": _handle_validate_envelope,
    "verify-journal": _handle_journal,
    "show-run": _handle_journal,
    "evidence-bundle": _handle_evidence_bundle,
    "reliability": _handle_reliability,
    "replay": _handle_replay,
    "project": _handle_project,
    "recover": _handle_recovery,
    "provider-dossier": _handle_provider_dossier,
    "bench-intake": _handle_bench_intake,
    "release-readiness": _handle_release_readiness,
    "pilot-evidence": _handle_pilot_evidence,
    "naming-readiness": _handle_naming_readiness,
    "adapter-conformance": _handle_adapter_conformance,
    "mcp": _handle_mcp,
}


def main(argv: Sequence[str] | None = None) -> None:
    """Parse an optional argument sequence and dispatch one local command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMAND_HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        return
    handler(args)


if __name__ == "__main__":
    main()
