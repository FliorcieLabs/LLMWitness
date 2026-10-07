# Local API reference

All examples are localhost-only. If `LLMWITNESS_INGEST_TOKEN` is configured, send `Authorization: Bearer <token>` to ingestion routes.

- `POST /ingest/sdk` — bounded SDK telemetry.
- `POST /ingest/gateway` — bounded gateway audit telemetry.
- `POST /ingest/extension` — bounded browser telemetry.
- `POST /ingest/seal` — create one receipt for an existing correlation ID.
- `GET /ingest/session/{correlation_id}` — inspect an in-memory local session.
- `GET /health` — process health.
- `POST /v1/chat/completions` — bounded JSON development proxy.

Correlation IDs must be RFC 9562 UUIDv7 values. Creating a receipt prevents additional events for that in-memory session. Receipt creation can fail if storage fails or the target file already exists.

These are `0.x` APIs and may evolve in later releases with documented release notes.

## Unreleased service routes

- `GET /ingest/sessions` — summaries of the sessions currently in memory.
- `POST /v1/messages` — bounded Anthropic Messages development proxy.
- `GET /health` on the gateway — liveness plus delivered, failed, spooled and replayed telemetry counts.
- Both proxy routes pass a `"stream": true` response through as server-sent events. The audit copy holds the scrubbed assembled text, token usage and tool names.
- `POST /ingest/sdk` also accepts `provider`, `model`, `latency_ms`, `input_messages`, `estimated_cost_usd` and `error`.
- Receipts are version 2: the signature also covers `receipt_version` and `chain` (`index`, `previous_receipt_hash`). Version 1 receipts still verify.
- Request bodies are limited by the bytes received, not only by `Content-Length`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLMWITNESS_SESSION_DB` | `.llmwitness/sessions.db` | Unsealed-session store; `off` keeps sessions in memory only |
| `LLMWITNESS_SESSION_IDLE_TTL_SECONDS` | `86400` | Idle time before an unsealed session may be dropped at the limit |
| `LLMWITNESS_KEY_DIR` | `.llmwitness/keys` | Persistent signing key and HMAC secret |
| `LLMWITNESS_EPHEMERAL_KEYS` | unset | Set to `1` to keep per-process keys |
| `LLMWITNESS_TRUSTED_FINGERPRINT` | unset | Signer that `llmwitness verify` requires |
| `LLMWITNESS_SPOOL_DIR` | `.llmwitness/spool` | Undelivered telemetry; `off` disables spooling |
| `LLMWITNESS_SCRUB_RULES_FILE` | unset | JSON file of extra patterns and sensitive field names |
| `LLMWITNESS_PRICING_FILE` | unset | JSON price table for cost estimates |
| `UPSTREAM_ANTHROPIC_URL` | `https://api.anthropic.com` | Upstream for `/v1/messages` |

## Unreleased commands

| Command | What it does |
| --- | --- |
| `llmwitness serve` | Runs ingestion and the gateway in one process |
| `llmwitness keygen` | Creates the persistent signing key; never overwrites one |
| `llmwitness seal ID` | Seals one run into a receipt |
| `llmwitness list` | Lists local receipts, newest first (`--json`) |
| `llmwitness show RECEIPT_OR_ID` | Prints a run's timeline, or writes a static page with `--html` |
| `llmwitness diff A B` | Shows where two runs diverged; exits 1 when they differ |
| `llmwitness verify-chain` | Checks the receipt hash chain for gaps and replaced receipts |
| `llmwitness export-otel RECEIPT_OR_ID` | Writes OTLP/JSON trace data (`--output`) or posts it to a collector (`--endpoint`) |
| `llmwitness timestamp RECEIPT_OR_ID --tsa-url URL` | Stores an RFC 3161 timestamp token; sends only a SHA-256 digest to the authority |

A scrub rules file looks like this:

```json
{
  "patterns": [{"name": "employee_id", "regex": "\\bEMP-\\d{6}\\b"}],
  "sensitive_fields": ["session_cookie"]
}
```

A pricing file maps a model name, or a prefix of one, to prices per million tokens. No prices are built in:

```json
{"your-model": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}
```

## Unreleased Python and CLI surfaces

- `llmwitness.envelope.ExecutionEnvelope` validates schema v0.1, UUIDv7 identifiers, RFC 3339 timestamps, hash references, and evidence-required `VERIFIED` or `COMPENSATED` states. Its checked-in JSON Schema is `schemas/execution-envelope-v0.1.schema.json`.
- `llmwitness.journal.SQLiteJournalStore` appends, scans, verifies, flushes, and signs per-run local journal heads. Safe Effect preparation and idempotency reservation commit together; inspection can use SQLite read-only mode.
- `llmwitness.journal.WritableEffectJournal` and `ReadOnlyJournal` are narrow structural ports for Safe Effects and recovery planning. Structural conformance does not by itself prove durability or integrity.
- `llmwitness.artifacts.FileArtifactStore` owns atomic local content-addressed artifact publication and integrity-checked reads.
- `llmwitness.receipts.ReceiptService` issues signed journal heads through a narrow signer port. Compatibility imports from `llmwitness.journal` remain available.
- `llmwitness.contracts` and `llmwitness.authority` own contract and permission evaluation.
- `llmwitness.effects` and `llmwitness.transactions` own Safe Effects, verification, compensation, and Saga-style coordination.
- `llmwitness.state` and `llmwitness.replay` own state classification and side-effect-safe replay.
- `llmwitness.runtime` preserves compatibility by re-exporting those phase-specific APIs.
- `llmwitness.adapters.default_adapter_registry()` creates dependency-isolated
  mapping translators for Python, OpenTelemetry, LangChain, LangGraph, OpenAI
  Agents, MCP, CrewAI, AutoGen, and LlamaIndex.
- `llmwitness.adapter_conformance.run_adapter_conformance()` runs one
  deterministic normalized-mapping equivalence fixture across those translators
  and emits per-dimension results. Passing does not certify a native framework
  SDK, hook, version, or deployment.
- `llmwitness.reliability` runs deterministic scenarios and emits JSON, JUnit, and Markdown.
- `llmwitness.effect_conformance.EffectAdapterConformanceHarness` exercises local adapter fixtures through the existing Safe Effect runner and emits per-dimension JSON/Markdown reports. Passing is not live-provider or production certification.
- `llmwitness.planning.EffectPlanner` evaluates existing authority and contract inputs without a journal, adapter, provider call, or raw request/value export. A plan is an explanation, not authorization.
- `llmwitness.provider_readiness.validate_provider_dossier` validates a strict
  metadata-only dossier for human review. The resulting JSON/Markdown reports
  omit the dossier body and cannot approve or certify a provider adapter.
- `llmwitness.bench_governance.validate_bench_dataset_intake` validates a
  strict metadata-only governance manifest. Reports expose structural findings
  and fixed pending-role identifiers without reading records, holdout content,
  or submitted review documents.
- `llmwitness.release_readiness.validate_release_readiness` validates a strict
  metadata-only FTDR input. The report always sets `publication_authorized` to
  false and never reproduces submitted account/evidence references.
- `llmwitness.passport` provides a local reference issuer, verifier, and
  `PassportAuthorityMapper`. Callers supply the trusted issuer public key,
  current credential status, exact invocation, and a replay guard; a successful
  verification yields minimized `EvidenceReference` material that can be placed
  on `EffectContext.authority_evidence`. It only restricts the existing
  `AuthorityPolicy`; it cannot dispatch, approve R3, verify an effect, or prove
  an organization/provider claim.
- `llmwitness.reliability` can create a versioned baseline and compare later deterministic corpus results. `llmwitness reliability --baseline PATH` exits nonzero for removed, invariant-changed, regressed, or newly failing scenarios; durations do not decide policy. `--write-baseline PATH` is an explicit local maintenance operation and cannot be combined with `--baseline`.
- `llmwitness.projects` validates, bootstraps, and runs only the local refund and CRM reference projects.
- `llmwitness.recovery.RecoveryPlanner` derives a read-only recovery inbox from verified journal chains through `ReadOnlyJournal`.
- `llmwitness.evidence_bundle.export_evidence_bundle(...)` exports one verified
  run without artifact bytes or source paths; `verify_evidence_bundle(...)`
  validates the bounded archive, canonical envelopes, manifest, and complete
  hash chain offline. Passing verification establishes local byte consistency,
  not external truth or immutable storage.

Run `llmwitness --help` for schema, envelope, journal, evidence-bundle, project,
recovery, replay, reliability, provider-dossier, bench-intake,
release-readiness, pilot-evidence, naming-readiness, adapter-conformance, and MCP
commands. `python -m
llmwitness.mcp_server` starts a newline-delimited JSON-RPC STDIO server. Its
tools validate envelopes, verify and inspect local journals, list unresolved
recovery items, plan recovery, list adapters, and run the side-effect-free
reliability corpus.
