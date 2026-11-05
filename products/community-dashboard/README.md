# Community evidence dashboard

`FL-DASH-001` is an interactive local report viewer for LLMWitness Community,
maintained as part of the LLMWitness repository.
It complements the existing offline Faultboard; it is not a hosted control plane.

## Run

From the repository root, using Python 3.10 or later:

```powershell
python scripts/preview-community-dashboard.py
```

Open http://127.0.0.1:8765 in a current browser. Use `--port 8766` if needed.
No build, account, paid API or JavaScript dependency is required. Stop with Ctrl+C.
Use the loopback preview rather than opening HTML directly, because ES modules
require a compatible browser origin.

## Features

- Explicit synthetic demo; empty workspace by default.
- Import Reliability JSON, adapter-conformance JSON, Proof Card JSON and
  normalized metadata-only workflow JSONL from the current SDK.
- Search labels and UUIDv7 correlation, filter declared status, navigate report
  kinds, and inspect minimized metadata and available timing.
- Responsive layout, light/dark themes, and local minimized snapshot export.

Snapshots are review summaries, not re-importable reports or verification
artifacts. Demo data is illustrative, not measured benchmarks. Source SHA-256
references identify imported bytes, not authenticated signers or external truth.
The viewer does not re-run checks. Unknown, failed and not-checked remain distinct;
missing timing is **not reported**, never zero. There is no universal score.

## Privacy and boundary

Imports remain in browser memory: no uploads, analytics, external assets,
browser storage, provider calls, journal access or recovery execution. Raw source
labels, prompts, model names, tool arguments and error prose are omitted.
Review inputs and exported metadata before sharing; minimization is not DLP.
Only profile 0.1 is supported: four files maximum, one per kind, 1 MiB each,
1,000 rows per report and 64 JSON container levels. Duplicate keys, nonfinite
values and contradictory counts fail closed. The server exposes five fixed
assets on loopback under a restrictive CSP, not repository files or data APIs.
This is not a production server, live telemetry, multi-user, Cloud/Enterprise,
compliance or universal framework-support claim.

## Plugins and skills

Figma, MagicPath and Canva were checked as installed/enabled. They are optional
for prototypes, layout exploration and branding; account connections were not
tested and no reports were sent to them. Architecture, professional coding,
security and Sites local-preview skills guided this dependency-free local scope.
None of those plugins is needed to run the dashboard.

## Verification

```powershell
node --test products/community-dashboard/tests/report-model.test.mjs
python -m pytest tests/test_community_dashboard_preview.py -p no:cacheprovider --basetemp .llmwitness/dashboard-test-run
python -m ruff check scripts/preview-community-dashboard.py scripts/verify-community-dashboard-reports.py tests/test_community_dashboard_preview.py
```

Use a new temporary test directory each run. Node 22+ is needed only for tests.
The optional `scripts/verify-community-dashboard-reports.py` generates actual
producer fixtures: run it with Python `-I` from a separate environment containing
the current installed LLMWitness package and provide a new `--output-dir`.
It executes deterministic local checks, not live providers.

Public architecture: `docs/adr/0029-local-community-dashboard.md`; `TASK-329`.
Portfolio planning specifications remain outside the public LLMWitness repository.

Browser regression procedure: load the synthetic demo, import
`tests/invalid-report.json`, and confirm the rejection message, four retained
reports and unchanged rows. This fixture intentionally uses an unsupported
version; never present it as a supported report.
