# LLMWitness roadmap

Roadmap items are plans, not commitments or currently available capabilities.

## Community — available in 0.1.0

- Bounded Python telemetry queue and UUIDv7 correlation.
- Localhost JSON gateway with best-effort audit-copy scrubbing.
- In-memory local ingestion and per-session tamper-evident receipts.
- Browser SDK, opt-in extension, and receipt-verification CLI.

Near-term Community work: stronger schema limits and reproducible installed-package tests. Durable delivery options, OpenTelemetry export, consent controls for browser capture and gateway streaming are implemented on the development branch and not yet released.

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
