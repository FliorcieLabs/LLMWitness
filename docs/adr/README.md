# Community architecture decision index

This public index preserves the existing implemented Community constraints. It does not accept new identity, live-provider, release, Cloud or Enterprise decisions. Read it together with [Community behavior](../FLIORCIE_COMMUNITY.md) and [architecture](../ARCHITECTURE.md).

1. LLMWitness is the evidence plane; reliability components do not replace agent orchestration.
2. UUIDv7 run correlation and OpenTelemetry interoperability remain cross-component contracts.
3. Execution Envelope v0.1 is the canonical reliability record; unsupported versions fail explicitly.
4. Best-effort telemetry and consequential-effect journaling have separate stores and guarantees.
5. Effects use scoped idempotency, with `PREPARED` and its reservation committed atomically.
6. `UNKNOWN` requires reconciliation or manual review, never a blind retry or normalization to failure.
7. `VERIFIED` requires the owning verification path and evidence; a signature or model judgment alone does not establish provider truth.
8. Transactions are Saga-like, with explicit compensation and compensation verification, not ACID rollback across external providers.
9. Internal state restoration never implies reversal of external effects. Replay is side-effect-free by default; exact replay needs a determinism boundary and live execution needs an application executor and explicit policy.
10. Authority, contracts, effects, transactions, state, replay, artifacts and receipt signing have separate owners. Compatibility and Community facades own no second state machine.
11. Recovery and portable bundle export depend on read-only journal ports. Framework integrations are optional edge translators; deterministic conformance fixtures do not certify native framework SDKs.
12. Receipts and journals establish local tamper evidence only. Signer trust needs separate configuration; external truth, immutability and deletion-proof retention are not inferred.
13. Reliability baselines use stable scenario identity and explicit dimension differences, not a universal score or a timing-based safety gate.
14. The local Passport reference restricts existing Runtime authority; it cannot independently execute or verify an external effect.
15. Community remains single-user localhost. Cloud/Enterprise code stays outside this repository and publication remains an explicitly authorized maintainer operation.

The consolidated audit fixes restore these invariants at existing boundaries; they do not introduce a new protocol or distributed architecture. A future incompatible safety or public-contract change must include a numbered ADR documenting context, alternatives, consequences, migration and acceptance evidence before implementation.
