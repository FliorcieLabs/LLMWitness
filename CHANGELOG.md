# Changelog

## Unreleased

- Persisted unsealed ingestion sessions in local SQLite so a restart no longer loses them; sealed sessions are evicted at the session limit instead of blocking new ones.
- Persisted the signing key and HMAC secret on first service start, added `llmwitness keygen`, and made `llmwitness verify` require the local or configured signer by default.
- Fixed `Ed25519KeyManager` ignoring an explicit public key when `LLMWITNESS_PRIVATE_KEY_PEM` was set.
- Added receipt version 2, which signs a hash link to the previous receipt, and `llmwitness verify-chain`.
- Added SDK and gateway delivery retry with an on-disk spool, moved SDK warnings from `print()` to `logging`, and added gateway `/health` telemetry counters.
- Required a Luhn checksum before redacting a card number, added email and formatted phone-number scrubbing, and added custom scrub patterns, field names and scrubber hooks.
- Enforced the ingestion body limit on bytes received rather than on `Content-Length` alone.
- Added an Anthropic `/v1/messages` gateway route and server-sent-event pass-through for both gateway routes.
- Captured model, prompt, latency, errors and optional cost estimates in the SDK; added async, streaming and Anthropic client wrapping, `tracker.seal()` and `auto_seal`.
- Added `llmwitness serve`, `seal`, `list`, `show`, `diff`, `export-otel` (OTLP/JSON) and `timestamp` (RFC 3161 token request).
- Added a `@witness` decorator and a LangChain callback handler.
- Gated browser-extension capture behind a per-site allow-list with a visible recording notice.
- Restored the project schema, sample projects and reliability baseline that the test suite reads.
- Raised dependency minimums to releases without known advisories (`fastapi>=0.134.0`, `starlette>=1.3.1`, `cryptography>=50.0.0`, `pydantic>=2.10.0`) and added a `langchain` extra.
- Bounded the email scrubbing pattern so hostile text cannot cause quadratic backtracking.
- Restricted the session database, spool and key directory to their owner.
- Moved six identical atomic-write helpers, UUIDv7 validation and delivery-status classification to single shared functions.
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
- Redacted tuple, set, and frozenset values that previously reached telemetry unscrubbed.
- Rejected non-finite JSON number literals with 422 instead of failing while rendering the validation error.
- Stopped allow-list placeholder tokens supplied in model output from being rewritten into allow-listed terms.
- Stopped an inline `data:image` payload from swallowing the text that follows it.
- Mapped receipt-directory creation failures to 507 alongside the existing write failures.
- Added edge-case suites for redaction, key handling, the SDK queue and lifecycle, both services, and the CLI.

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
