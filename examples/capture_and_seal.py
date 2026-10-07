"""Record one model call and seal the run into a local receipt.

Start the services first with ``llmwitness serve``. Set
``LLMWITNESS_MOCK_UPSTREAM=true`` before starting them to try this without an
API key; the gateway then answers with a canned reply.
"""

from openai import OpenAI

from llmwitness import LLMWitnessTracker

tracker = LLMWitnessTracker(ingestion_url="http://127.0.0.1:8000")
# Pointing the client at the gateway adds the gateway's audit copy to the run.
client = tracker.wrap_openai_client(
    OpenAI(base_url="http://127.0.0.1:8011/v1", api_key="local-development")
)

with tracker.trace_session("capture-and-seal", auto_seal=True) as correlation_id:
    client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Say hello."}],
    )

tracker.shutdown()
if tracker.last_receipt:
    print("Receipt:", tracker.last_receipt["receipt_file"])
    print("Inspect it with: llmwitness show", correlation_id)
print(tracker.stats())
