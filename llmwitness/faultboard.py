"""Offline visual report for the existing deterministic Reliability corpus.

Faultboard is a presentation edge. Reliability owns scenarios and policy.
"""

from __future__ import annotations

import argparse
import html
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llmwitness.reliability import (
    ReliabilityReporter,
    ScenarioResult,
    reliability_summary,
    run_reliability_corpus,
)

MAX_SCENARIOS = 128
MAX_LABEL_LENGTH = 1_024
LIMITATIONS = (
    "Deterministic local fixtures only. Passing scenarios do not establish "
    "external provider truth, production readiness, compliance, or a universal safety score."
)

_STYLE = """
:root{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:#08111f;color:#e6edf7;min-height:100vh}
.shell{max-width:1100px;margin:auto;padding:48px 24px 72px}
.eyebrow{color:#64d8c0;font-size:.78rem;font-weight:800;letter-spacing:.18em;text-transform:uppercase}
h1{font-size:clamp(2.4rem,6vw,4.5rem);letter-spacing:-.06em;margin:10px 0 12px}
.lead{font-size:1.12rem;max-width:760px;color:#a8b8cc;line-height:1.6}
.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:32px 0}
.metric{background:#122137;border:1px solid #253a54;border-radius:18px;padding:20px}
.metric strong{font-size:2.2rem;display:block}.metric span{color:#a8b8cc}
.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin:40px 0 14px}
h2{font-size:1.35rem;margin:0}.hint{color:#90a3bb;font-size:.9rem}
.list{display:grid;gap:10px}.card{background:#101d30;border:1px solid #28415c;border-radius:14px}
.card.fail{border-color:#a34c61;background:#2b1723}
summary{cursor:pointer;display:flex;align-items:center;gap:12px;padding:18px;list-style:none}
summary::-webkit-details-marker{display:none}summary:focus-visible{outline:2px solid #8ddfd0;outline-offset:3px}
.badge{font-size:.7rem;font-weight:900;letter-spacing:.1em;color:#07221c;background:#72ddbe}
.badge{padding:6px 9px;border-radius:6px}
.fail .badge{background:#ff9eac;color:#36101a}.name{font-weight:750;overflow-wrap:anywhere}
.duration{color:#93a7c0;margin-left:auto;white-space:nowrap;font-variant-numeric:tabular-nums}
.card p{padding:0 18px 18px;margin:0;color:#b7c6da;line-height:1.55;overflow-wrap:anywhere}
.note{border-left:3px solid #4e9ca4;padding:14px 18px;background:#102133;color:#b7c6da;line-height:1.55;margin-top:32px}
footer{margin-top:30px;color:#7990ab;font-size:.86rem}
@media(max-width:640px){.shell{padding:28px 16px 50px}.metrics{grid-template-columns:1fr}}
@media(max-width:640px){summary{flex-wrap:wrap}.duration{width:100%;margin-left:0}}
"""


@dataclass(frozen=True)
class AtlasRun:
    html_path: Path
    summary: dict[str, Any]


def _validate_results(results: Sequence[ScenarioResult]) -> None:
    if not results or len(results) > MAX_SCENARIOS:
        raise ValueError("Faultboard requires 1 to 128 scenarios")
    for result in results:
        if (
            not result.name.strip()
            or not result.invariant.strip()
            or len(result.name) > MAX_LABEL_LENGTH
            or len(result.invariant) > MAX_LABEL_LENGTH
            or not math.isfinite(result.duration_ms)
            or result.duration_ms < 0
        ):
            raise ValueError("Faultboard scenario has invalid display fields")


def render_faultboard(results: Sequence[ScenarioResult]) -> str:
    """Render escaped labels and invariants; omit arbitrary failure details."""
    _validate_results(results)
    summary = reliability_summary(list(results))
    cards: list[str] = []
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        css_class = "pass" if result.passed else "fail"
        cards.append(
            f'<details class="card {css_class}"><summary>'
            f'<span class="badge">{status}</span>'
            f'<span class="name">{html.escape(result.name, quote=True)}</span>'
            f'<span class="duration">{result.duration_ms:.2f} ms</span>'
            "</summary>"
            f"<p><strong>Invariant:</strong> {html.escape(result.invariant, quote=True)}</p>"
            "</details>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" '
        'content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; '
        'base-uri &#39;none&#39;; form-action &#39;none&#39;">'
        "<title>Fliorcie Faultboard — local scenarios</title>"
        f'<style>{_STYLE}</style></head><body><main class="shell">'
        '<div class="eyebrow">Fliorcie Community · local reliability</div>'
        "<h1>Faultboard</h1>"
        '<p class="lead">Explore the safety invariant behind each deterministic '
        "failure scenario. Open a card to see what the local test checks.</p>"
        '<section class="metrics" aria-label="Scenario counts">'
        f'<div class="metric"><strong>{summary["sample_count"]}</strong><span>Scenarios</span></div>'
        f'<div class="metric"><strong>{summary["passed"]}</strong><span>Passed</span></div>'
        f'<div class="metric"><strong>{summary["failed"]}</strong><span>Failed</span></div>'
        "</section>"
        '<div class="section-head"><h2>Scenario evidence</h2>'
        '<span class="hint">Open a card to inspect its invariant</span></div>'
        f'<section class="list" aria-label="Scenarios">{"".join(cards)}</section>'
        f'<aside class="note">{html.escape(LIMITATIONS)}</aside>'
        "<footer>Generated offline from Fliorcie Reliability. "
        "No external assets or account required.</footer></main></body></html>\n"
    )


def run_faultboard(
    output_dir: Path,
    *,
    scenario_runner: Callable[[], list[ScenarioResult]] | None = None,
) -> AtlasRun:
    """Write existing reports and one HTML view in a fresh local directory."""
    absolute_output = output_dir.absolute()
    if any(path.is_symlink() for path in (absolute_output, *absolute_output.parents)):
        raise ValueError("Faultboard output path must not contain a symlink")
    output_dir.mkdir(parents=True, exist_ok=False)
    results = (scenario_runner or run_reliability_corpus)()
    rendered = render_faultboard(results)
    summary = ReliabilityReporter().report(results, output_dir=output_dir)
    html_path = output_dir / "faultboard.html"
    with html_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    return AtlasRun(html_path=html_path, summary=summary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    output_dir = args.output_dir or Path(".llmwitness/faultboard") / datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")
    try:
        result = run_faultboard(output_dir)
    except (OSError, ValueError) as exc:
        print(f"Faultboard could not write the report: {exc}", file=sys.stderr)
        return 2
    print(f"Faultboard: {result.html_path}")
    print(
        f"Scenarios: {result.summary['sample_count']}; "
        f"passed: {result.summary['passed']}; failed: {result.summary['failed']}"
    )
    return 1 if result.summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
