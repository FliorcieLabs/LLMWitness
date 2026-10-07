# Community architecture

```text
Python SDK ───────────────┐
Browser component ───────┼─> local ingestion memory ─> per-session signed receipt file
Local JSON gateway ──────┘
        │
        └─> upstream chat-completions endpoint
```

Every path accepts the same UUIDv7 correlation identifier so callers can correlate events across components. Components generate independent identifiers when the caller does not propagate one. The gateway scrubs only the telemetry copy and returns the upstream response body to the caller unchanged. Ingestion scrubs received events again because caller-supplied fields are not trusted.

On the development branch, ingestion mirrors unsealed sessions to a local SQLite file, the SDK and gateway spool undelivered telemetry to local disk, each receipt links to the previous one, and the gateway also proxies Anthropic Messages requests and server-sent-event streams.

The default services are single-process and in-memory. Receipt files are tamper-evident local artifacts, not durable immutable storage. Authentication, tenancy, distributed coordination, retention enforcement, streaming, and high availability are outside the Community boundary.

## Unreleased reliability layer

The development branch adds a separate consequential-effect lane. `ExecutionEnvelope` is the canonical record, `SQLiteJournalStore` persists hash-linked transitions and idempotency records with WAL/full synchronization, `FileArtifactStore` owns content-addressed local artifacts, and `ReceiptService` owns signed journal-head issuance. `SafeEffectRunner` coordinates authority, contracts, prepare, execute, authoritative verification, and explicit compensation. This does not make the telemetry endpoints durable: their existing queue and in-memory session behavior remains best-effort.

Framework adapters translate event mappings and preserve UUIDv7 run identifiers and OpenTelemetry trace identifiers. Replay returns recorded envelopes without live writes by default. The local MCP server exposes validation, inspection, recovery planning, and deterministic reliability scenarios; it does not expose an effect-execution tool.

`llmwitness.community` sits at the outer composition edge. It calls the
validated local project runner, opens the resulting SQLite journal read-only
for recovery inspection, and invokes the existing Reliability corpus/reporter.
Core evidence and runtime modules must not import it. The facade does not own a
second state machine, recovery executor, replay path, or provider integration.
