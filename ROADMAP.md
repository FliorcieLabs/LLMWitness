# LLMWitness roadmap

Roadmap items are plans, not commitments or currently available capabilities.

## Community — available in 0.1.0

- Bounded Python telemetry queue and UUIDv7 correlation.
- Localhost JSON gateway with best-effort audit-copy scrubbing.
- In-memory local ingestion and per-session tamper-evident receipts.
- Browser SDK, opt-in extension, and receipt-verification CLI.

Near-term Community work: stronger schema limits and reproducible installed-package tests. Durable delivery options, OpenTelemetry export, consent controls for browser capture and gateway streaming are implemented on the development branch and not yet released.

## Community adoption — unreleased development work

- Metadata-only Codex JSONL, Claude Code hook and framework-neutral observation
  capture, with bounded offline CLI import and explicit UUIDv7 correlation.
- A real local LangGraph example and optional callback tests, without a paid model.
- [Workflow adoption guide](docs/WORKFLOW_ADOPTION.md) and
  [connected Community use cases](docs/COMMUNITY_USE_CASES.md).

- Opt-in bounded metadata-only LangChain/LangGraph model/tool callbacks.
- Plain sync/async OpenAI Responses metadata capture, with native local mock tests.
- Offline minimized report Proof Cards with strict parsing and no-overwrite output.
- [Extension usage and limits](docs/ADOPTION_EXTENSIONS.md).

These are implemented locally but unreleased, not universal framework support, a new
orchestrator, accepted Passport decisions or evidence of outside adoption.
Provider credentials/approvals, independent pilot evidence, Bench governance,
release controls and naming/IP clearance remain human-controlled gates.

## Cloud — planned and proprietary

- Managed ingestion and gateway.
- Team visualization, session analytics, and operational management.

No managed Cloud service is currently available, and its implementation is not included here.

## Enterprise — planned and proprietary

- Multi-tenancy, RBAC, SSO/SAML, and policy enforcement.
- Distributed storage/cache, high availability, and multi-region operation.
- Object-lock retention and managed HSM/KMS integration.
- Evidence export and reporting workflows that may support customer compliance programs.

These features are not implemented in this repository. Reporting would not itself certify or guarantee compliance.
