# Changelog

## Unreleased

- Added Execution Envelope v0.1, durable local hash-linked journals, content-addressed evidence, and signed journal heads.
- Added contracts, authority/risk enforcement, Safe Effects, persistent idempotency, Saga-style compensation, state snapshots, replay guards, and dependency-isolated framework adapters.
- Separated contracts, authority, Safe Effects, transactions, state, and replay
  into owning modules while preserving `llmwitness.runtime` imports.
- Inverted Safe Effect and recovery planning dependencies onto narrow writable
  and read-only journal protocols while retaining SQLite as the local default.
- Moved artifact storage and signed-journal-head receipt services to owning
  modules while preserving journal compatibility imports.
- Added deterministic reliability reports and a local STDIO MCP server.
- Added deterministic verifier-disagreement and tool-output-poisoning scenarios,
  bringing the Community corpus to fourteen named safety checks.
- Added deterministic provider-rate-limit-before-dispatch coverage; the local
  corpus now has fifteen named safety checks.
- Added a typed Community SDK facade that composes the validated local project
  runner, read-only recovery inspection, and three-format Reliability reports.
- Enforced RFC 9562 UUIDv7 validation at caller-controlled Python and browser boundaries.
- Added in-memory authenticated browser ingestion and observable browser delivery failures.
- Made receipt publication atomic, no-overwrite, and retryable after write failures.
- Bounded gateway audit parsing for large JSON responses and corrected byte truncation metadata.
- Excluded generated benchmark results from source distributions.
- Disabled automatic package publication; release workflows now validate artifacts only.

## 0.1.0 — 2026-08-07

- Renamed the project and import namespace to LLMWitness.
- Defined Community as a single-user localhost toolkit with explicit limitations.
- Added canonical Ed25519 receipt signing and self-contained verification.
- Added per-session receipt files and fail-closed persistence errors.
- Added UUIDv7 validation, event/session bounds, and storage-boundary scrubbing.
- Removed the unsafe shared semantic cache and response-mutating audit behavior.
- Added a packaged `llmwitness` CLI.
- Removed duplicate compatibility modules, stale generated benchmark results, and unsupported deployment material.
- Replaced unsupported privacy, immutability, performance, compliance, and production claims with explicit limitations.
