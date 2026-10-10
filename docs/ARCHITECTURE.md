# Community architecture

```text
Python SDK ── queue/retry/spool ─┐
Browser bridge / extension ─────┼─> validate + scrub ─> local session SQLite
Local gateway ── retry/spool ───┘                         │
        │                                         memory view
        └─> configured upstream                         │ seal
                                              signed linked receipt file

Local project / Community facade
        └─> authority + contracts ─> SafeEffectRunner ─> provider protocol
                                          │                 │ verification
                                     journal SQLite <───────┘
                                          └─> artifacts / receipt / offline bundle
```

Every path accepts the same UUIDv7 correlation identifier so callers can correlate events across components. Components generate independent identifiers when the caller does not propagate one. The gateway scrubs only the telemetry copy and returns the upstream response body to the caller unchanged. Ingestion scrubs received events again because caller-supplied fields are not trusted.

On the consolidated development branch, ingestion mirrors unsealed sessions to a local SQLite file by default. The SDK and gateway spool undelivered telemetry to local disk and each receipt links to the previous one. These additions are unreleased, not an assertion about the published `0.1.0` package. Experimental provider-specific SSE pass-through exists for local development; it does not establish complete streaming capture or broad provider compatibility.

The services remain single-process, single-user localhost tools. SQLite persistence can be disabled with `LLMWITNESS_SESSION_DB=off`; queue delivery and disk recovery remain best-effort. Receipt files are tamper-evident ordinary local files, not immutable storage. Optional shared-token checks are not an identity or tenancy system. Distributed coordination, enterprise authentication, guaranteed retention, high availability, and comprehensive streaming support are outside the Community boundary.

## Local persistence and failure boundaries

- An event is written to the session database before the in-memory event list is updated. Storage errors return a generic HTTP 507 without exposing storage diagnostics or acknowledging an unpersisted event.
- A published receipt prevents reopening that UUIDv7 session, including after memory eviction or process restart. Do not delete a receipt and assume this provides a separate permanent revocation ledger.
- Receipt publication is the seal commit point. If later database cleanup fails, sealing remains successful; startup reconciles leftover sessions against existing receipt files.
- A spool worker claims one batch by atomic rename, refreshes its lease, and retains undelivered entries by atomic replacement. Write failure preserves the original batch. Recovery can duplicate events; it is not exactly-once delivery. The stale-claim timeout is a recovery heuristic, not a distributed lock or protection for arbitrarily long replay attempts.
- Spool capacity is a local best-effort bound, not a cross-process quota transaction. Disk exhaustion, queue overflow, torn appends, unavailable storage, and replay into an already sealed session can still lose telemetry.
- Best-effort SDK recording must not replace a provider exception or break successfully delivered stream chunks when final recording fails.
- Receipt-chain verification detects broken links in the supplied chain, not deletion of its terminal suffix without an independently retained checkpoint. Signer identity requires a separately trusted fingerprint; a self-consistent embedded key does not establish external trust.

## Unreleased reliability layer

The development branch adds a separate consequential-effect lane. `ExecutionEnvelope` is the canonical record, `SQLiteJournalStore` persists hash-linked transitions and idempotency records with WAL/full synchronization, `FileArtifactStore` owns content-addressed local artifacts, and `ReceiptService` owns signed journal-head issuance. `SafeEffectRunner` coordinates authority, contracts, prepare, execute, authoritative verification, and explicit compensation. Telemetry sessions and consequential effects have different stores and semantics; local session persistence does not confer journal guarantees on telemetry or establish provider truth.

Framework adapters translate event mappings and preserve UUIDv7 run identifiers and OpenTelemetry trace identifiers. Replay returns recorded envelopes without live writes by default. The local MCP server exposes validation, inspection, recovery planning, and deterministic reliability scenarios; it does not expose an effect-execution tool.

`llmwitness.community` sits at the outer composition edge. It calls the
validated local project runner, opens the resulting SQLite journal read-only
for recovery inspection, and invokes the existing Reliability corpus/reporter.
Core evidence and runtime modules must not import it. The facade does not own a
second state machine, recovery executor, replay path, or provider integration.

## Ownership and extension rules

Keep transport validation in ingestion/gateway, delivery recovery in the spool/SDK, session persistence in `session_store`, and consequential-effect transitions in the runtime/journal modules. Depend on small provider and storage protocols; optional framework integrations stay at the outer edge. New behavior should add a regression at its boundary rather than another state machine in the facade or a speculative shared base class.

Artifact publication validates a competing existing blob before returning its content address. State restoration copies the replacement before clearing current state, and state diffs distinguish missing keys from present JSON null values. Halted transactions cannot dispatch further effects or become verified merely because their halt exception was caught. See [Community behavior](FLIORCIE_COMMUNITY.md) and the [decision index](adr/README.md) for phase contracts and explicit recovery semantics.

The local Passport reference profile, adapter fixtures, and deterministic reliability corpus are development aids. They do not replace a standards-based identity product, approved live refund/CRM providers, independent adoption evidence, or governed model evaluations. Cloud and Enterprise implementation remains excluded.
