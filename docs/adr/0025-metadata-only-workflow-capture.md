# ADR 0025: metadata-only workflow capture

Date: 2026-10-10. Status: bounded Community implementation decision.
Component: `FL-WIT-CAPTURE-001`; delivery task: `TASK-325`.

## Context

Codex CLI JSONL and Claude Code hooks expose useful workflow observations.
Existing mapping-adapter conformance covers normalized Runtime fixtures, not
these native formats. Converting ordinary observations into Execution Envelopes
would incorrectly infer authority, effect verification and durable guarantees.
Adoption requires a small usable integration edge, not another orchestrator.

## Decision

1. `workflow_capture` normalizes allowlisted native and generic metadata into
   the existing SDK telemetry payload. A narrow `TelemetrySink` protocol accepts
   `record_event`; it never owns delivery, receipt sealing or effect execution.
2. Require caller-supplied UUIDv7 across each workflow. Native thread, session,
   tool and event IDs are source-separated SHA-256 references, not identities
   or trust decisions. Hashing is pseudonymization, not anonymization.
3. Omit prompts, commands, tool arguments/results, paths, environment data and
   error messages. Source event names, item types and statuses use allowlists;
   malformed/unknown shapes fail with content-free diagnostics.
4. `capture_cli` reads bounded local input and emits ingestion-compatible
   metadata JSONL offline. Validate the entire bounded batch before output;
   never overwrite existing output files or install hooks automatically.
5. Codex input is JSONL; Claude hook input is one JSON object. Generic JSONL
   supports application-defined workflows through the same minimal contract.
6. Existing optional LangChain callbacks are reused by LangGraph. A real local
   graph with a fake model provides reproducible evidence without paid calls.

## Alternatives

Adding source names to mapping fixtures cannot establish native compatibility.
A new workflow engine duplicates framework ownership. Automatic hook ingestion
adds process-lifetime, retry and consent ambiguity; SDK delivery remains an
explicit application decision. Content capture by default increases exposure
and is unnecessary for this observation profile.

## Consequences and migration

This is additive; existing SDK, ingestion, Runtime, receipt and MCP contracts
remain unchanged. Native completion/failure are observations, never Runtime
`VERIFIED`/`FAILED`, issuer trust, provider truth or authorization. No durable
deduplication, ordering, full transcript capture or universal support is added.
Repeated observations remain repeated telemetry. Caller-controlled timestamps
represent observation time in offline payloads, not authoritative source-event
time. The existing SDK sink owns recording-time timestamps; supplying
`observed_at` to the bridge does not override persisted SDK event time.

Only explicit source profiles are supported. New native types require fixtures,
tests and a profile update; unsupported events cannot silently disappear.
Base installation gains no framework dependency. LangGraph is an optional extra.
Community remains single-user localhost; delivery is best effort and receipts
are local tamper-evident ordinary files. No publishing or hosted service is
authorized by this ADR.

## Acceptance evidence

Before verification: metadata omission, UUIDv7, numeric/type/size/malformed
input, no-output-on-validation-failure, output preservation, exact sink payload,
real LangGraph callbacks, isolated installed-wheel smoke and full quality gates.
The adoption guide records supported formats, versions tested and limitations.

CLI JSON nesting is additionally limited to 64 levels before decoding.
Exclusive file output requires hard-link support and fails closed where absent;
stdout is an alternative, not an atomic delivery guarantee. Cleanup failure
after output commit warns without invalidating the completed metadata export.
