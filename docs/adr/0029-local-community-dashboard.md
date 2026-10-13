# ADR 0029: Local read-only Community dashboard

Accepted 2026-10-11 for `FL-DASH-001` / `TASK-329`.

## Context

Faultboard offers an offline Reliability HTML report, not an interactive view
connecting metadata capture and other Community reports. A hosted team dashboard
would cross the deferred Cloud/Enterprise boundary and expose local evidence.

## Decision

Add a separate buildless browser-local report viewer under
`products/community-dashboard`. Report owners remain unchanged. Explicit file
selection projects bounded minimized metadata into memory. No provider actions,
recovery actions, upload, external assets or implicit storage. Missing/unknown/
failed declarations stay visible. Input digests and reported passes are not
authentication or evidence of external truth. Demo fixtures require user action
and remain labeled synthetic. Use semantic HTML, CSS and modules, avoiding a
framework/toolchain dependency for this small local scope.

## Alternatives and consequences

- Hosted Sites/React control plane: deferred; unnecessary operating/dependency
  cost and different authority/privacy requirements.
- Change Faultboard: rejected to preserve its static contract and existing tests.
- Figma/MagicPath design canvas: useful optional design tooling, not required
  runtime dependencies or reason to send private reports to external services.

The static viewer is not a live ingestion dashboard, credential verifier,
immutable storage, compliance system or production-ready hosted service. Runtime
state transitions, signature verification and dataset governance stay outside it.

## Acceptance

Metadata parser/security regressions, local static-server boundary tests and real
browser interaction checks. Evidence belongs in `.specs/04-delivery/verification-log.md`.
