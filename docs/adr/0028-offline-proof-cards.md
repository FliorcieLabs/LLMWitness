# ADR 0028: offline report Proof Cards

Date: 2026-10-11. Status: bounded implementation decision.
Task/component: `TASK-328`, `FL-WIT-CARD-001`.

A standard-library edge renderer projects bounded existing report JSON into
deterministic Markdown/JSON. It exposes only fixed report labels, declared
status/counts and report-byte digest references. Raw scenario names, detail,
error, input paths and identity content are not rendered. Optional authority
input is an explicit minimized summary, never a credential or trust decision.

A Proof Card does not authenticate reports or verify bundles, run Reliability,
contact providers, import WitnessID, or compute a safety score. Forged reports
can produce forged declarations; every artifact states this limitation.
Missing checks remain not-checked, failures/unknowns stay visible. CLI validates
all inputs before exclusive output creation, without replacing files.

Re-running verification inside the renderer creates a second owner. Raw report
embedding exposes unnecessary content. Hosted sharing violates Community scope.
Acceptance: real report compatibility, privacy/count/bounds regressions, stable
rendering, offline/no-overwrite behavior and installed-package smoke.
