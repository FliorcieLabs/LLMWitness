"""Generate real producer fixtures for Node/browser QA, not dashboard runtime."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    from llmwitness.proof_card import render_proof_card
    from llmwitness.workflow_capture import normalize_workflow_event

    import llmwitness
    from llmwitness.adapter_conformance import run_adapter_conformance
    from llmwitness.reliability import reliability_summary, run_reliability_corpus
    from llmwitness.utils import generate_uuidv7

    if not Path(llmwitness.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("use a separately installed distribution with Python -I")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    reliability = json.dumps(reliability_summary(run_reliability_corpus())).encode()
    adapters = run_adapter_conformance().model_dump_json().encode()
    card = render_proof_card({"reliability": reliability, "adapters": adapters}, format="json").encode()
    run_id = generate_uuidv7()
    workflow = "".join(
        json.dumps(normalize_workflow_event(source, event, run_id).to_sdk_payload()) + "\n"
        for source, event in (
            ("generic", {"type": "workflow.started"}),
            ("generic", {"type": "workflow.completed"}),
            ("codex-jsonl", {"type": "turn.completed"}),
            ("claude-code-hook", {"hook_event_name": "PostToolUse", "tool_name": "fixture"}),
        )
    ).encode()
    for name, payload in (
        ("reliability.json", reliability), ("adapters.json", adapters),
        ("proof-card.json", card), ("workflow.jsonl", workflow),
    ):
        with (args.output_dir / name).open("xb") as target:
            target.write(payload)
    print(json.dumps({"installed_distribution": True, "reports": 4, "provider_calls": 0}))


if __name__ == "__main__":
    main()
