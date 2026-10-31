# Examples

These examples show the smallest practical integration paths for LLMWitness Community.

- `openai_wrapper.py` demonstrates wrapping an OpenAI client.
- `langchain_callback.py` shows correlation binding for a LangChain-style chain run.
- `langgraph_example.py` runs a real optional local graph with a fake model/tool and correlated callbacks; no paid provider is needed.
- `workflow_metadata.jsonl` supplies synthetic metadata for the offline `llmwitness capture --source generic` quickstart.
- `autogen_crew.py` shows recording an AutoGen or CrewAI-style multi-agent task.
- `browser_extension_setup.md` shows how to load the optional browser extension and allow a site to be recorded.
- `capture_and_seal.py` wraps a client, records a run, seals it and prints the receipt path.
- `safe_effects.py` demonstrates deterministic response loss, `UNKNOWN` reconciliation, and exactly one in-memory refund execution.

The examples are intentionally minimal so they can be copied into real projects without extra framework overhead.

See [workflow adoption](../docs/WORKFLOW_ADOPTION.md) for source installation,
exact compatibility boundaries and the difference between content callbacks and
strict metadata-only capture.

