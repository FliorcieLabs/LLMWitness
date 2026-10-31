"""Privacy, lifecycle and application-fidelity regressions for opt-in edges."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from llmwitness.metadata_callbacks import make_metadata_langchain_handler
from llmwitness.proof_card import (
    MAX_REPORT_BYTES,
    ProofCardError,
    build_proof_card,
    render_proof_card,
)
from llmwitness.proof_card_cli import run_proof_card
from llmwitness.responses_capture import wrap_responses_client
from llmwitness.utils import generate_uuidv7


class Sink:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def record_event(self, **kwargs):
        if self.fail:
            raise ValueError("SECRET")
        self.events.append(kwargs)


def handler(sink, **kwargs):
    pytest.importorskip("langchain_core")
    return make_metadata_langchain_handler(sink, generate_uuidv7(), **kwargs)


def test_metadata_omits_content_and_rejects_duplicate_or_mismatched_ends():
    sink = Sink()
    callback = handler(sink)
    callback.on_llm_start({"SECRET": "SECRET"}, ["SECRET"], run_id="native-secret")
    callback.on_llm_start({}, [], run_id="native-secret")
    callback.on_tool_end("SECRET", run_id="native-secret")
    callback.on_llm_error(ValueError("SECRET"), run_id="native-secret")
    callback.on_llm_end("SECRET", run_id="native-secret")
    assert len(sink.events) == 1
    assert "SECRET" not in json.dumps(sink.events)
    assert "native-secret" not in json.dumps(sink.events)
    assert callback.stats() == {"duplicate_start": 1, "unmatched_end": 2, "pending": 0}
    assert sink.events[0]["agent_state"]["outcome"] == "failed"


def test_metadata_capacity_expiry_and_sink_failure():
    now = [0.0]
    callback = handler(
        Sink(fail=True), max_pending_runs=1, max_run_age_seconds=2, clock=lambda: now[0]
    )
    callback.on_tool_start({}, "SECRET", run_id="first")
    callback.on_tool_start({}, "SECRET", run_id="second")
    now[0] = 3
    callback.on_tool_end("SECRET", run_id="second")
    callback.on_tool_start({}, "SECRET", run_id="third")
    callback.on_tool_end("SECRET", run_id="third")
    assert callback.stats() == {
        "evicted": 1,
        "expired": 1,
        "unmatched_end": 1,
        "sink_failures": 1,
        "pending": 0,
    }


def test_metadata_concurrent_run_correlation():
    sink = Sink()
    callback = handler(sink)

    def observe(index):
        callback.on_tool_start({}, "SECRET", run_id=str(index))
        callback.on_tool_end("SECRET", run_id=str(index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(observe, range(200)))
    assert len(sink.events) == 200
    assert len({event["correlation_id"] for event in sink.events}) == 1
    assert callback.stats()["pending"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_pending_runs": True},
        {"max_pending_runs": 0},
        {"max_run_age_seconds": float("nan")},
        {"max_run_age_seconds": 0},
    ],
)
def test_metadata_invalid_options(kwargs):
    with pytest.raises(ValueError):
        handler(Sink(), **kwargs)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_responses_result_identity_privacy_and_stream_passthrough(asynchronous):
    response = SimpleNamespace(
        id="SECRET",
        status="completed",
        output_text="SECRET",
        model="SECRET",
        usage=SimpleNamespace(input_tokens=2, output_tokens=3),
    )
    received = []

    def sync_create(**kwargs):
        received.append(kwargs)
        return response

    async def async_create(**kwargs):
        return sync_create(**kwargs)

    client = SimpleNamespace(
        responses=SimpleNamespace(create=async_create if asynchronous else sync_create)
    )
    sink, run_id = Sink(), generate_uuidv7()
    wrap_responses_client(sink, client, lambda: run_id)
    original_wrapper = client.responses.create
    assert wrap_responses_client(sink, client, lambda: run_id) is client
    assert client.responses.create is original_wrapper
    headers = {"X-Correlation-ID": "incorrect", "custom": "kept"}
    call = client.responses.create(input="SECRET", extra_headers=headers)
    result = asyncio.run(call) if asynchronous else call
    assert result is response
    assert headers["X-Correlation-ID"] == "incorrect"
    assert received[0]["extra_headers"] == {
        "x-correlation-id": run_id,
        "custom": "kept",
    }
    assert sink.events[0]["prompt_tokens"] == 2
    assert "SECRET" not in json.dumps(sink.events)
    call = client.responses.create(stream=True)
    assert (asyncio.run(call) if asynchronous else call) is response
    assert len(sink.events) == 1
    with pytest.raises(ValueError):
        wrap_responses_client(Sink(), client, lambda: run_id)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("sink_fails", [False, True])
def test_responses_original_exception_survives(asynchronous, sink_fails, caplog):
    failure = asyncio.CancelledError("SECRET") if asynchronous else ValueError("SECRET")

    def sync_create(**kwargs):
        raise failure

    async def async_create(**kwargs):
        raise failure

    client = SimpleNamespace(
        responses=SimpleNamespace(create=async_create if asynchronous else sync_create)
    )
    sink = Sink(fail=sink_fails)
    wrap_responses_client(sink, client, generate_uuidv7)
    if asynchronous:
        # Catch inside the coroutine: on Python 3.10, asyncio.run() replaces a
        # cancelled task's CancelledError with a new one, hiding the wrapper's.
        async def invoke():
            try:
                await client.responses.create(input="SECRET")
            except BaseException as exc:  # noqa: BLE001 - asserting the exact object
                return exc
            return None

        raised = asyncio.run(invoke())
    else:
        with pytest.raises(type(failure)) as error:
            client.responses.create(input="SECRET")
        raised = error.value
    assert raised is failure
    assert "SECRET" not in caplog.text
    if not sink_fails:
        assert sink.events[0]["agent_state"]["status"] == "failed"


def report(passed=True):
    return {
        "schema_version": "0.1",
        "sample_count": 1,
        "passed": int(passed),
        "failed": int(not passed),
        "dimensions": [{"passed": passed, "detail": "SECRET", "name": "SECRET"}],
    }


def test_card_privacy_determinism_unknown_and_failure():
    reports = {
        "reliability": json.dumps(report(False)).encode(),
        "authority": b'{"schema_version":"0.1","status":"unknown","credential":"SECRET"}',
    }
    card = build_proof_card(reports)
    assert card["checks"]["reliability"]["status"] == "failed"
    assert card["checks"]["authority"]["status"] == "unknown"
    assert card["checks"]["bundle"]["status"] == "not_checked"
    for format in ("json", "markdown"):
        output = render_proof_card(reports, format=format)
        assert output == render_proof_card(reports, format=format)
        assert "SECRET" not in output
        assert "not authenticated" in output


@pytest.mark.parametrize(
    "data",
    [
        b"[]",
        b'{"schema_version":"0.1","schema_version":"0.1"}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b"[" * 65 + b"]" * 65,
        b"x" * (MAX_REPORT_BYTES + 1),
        b"\xff",
        b"{}",
    ],
    ids=["array", "duplicate", "nan", "infinite", "depth", "size", "unicode", "empty"],
)
def test_card_rejects_malformed_bounded_inputs(data):
    with pytest.raises(ProofCardError):
        build_proof_card({"reliability": data})


@pytest.mark.parametrize(
    "change",
    [
        {"passed": True},
        {"sample_count": 2},
        {"passed": 0},
        {"schema_version": "future"},
    ],
)
def test_card_rejects_inconsistent_counts(change):
    value = {**report(), **change}
    with pytest.raises(ProofCardError):
        build_proof_card({"reliability": json.dumps(value).encode()})


def test_card_real_reports():
    from llmwitness.adapter_conformance import run_adapter_conformance
    from llmwitness.reliability import reliability_summary, run_reliability_corpus

    reports = {
        "reliability": json.dumps(
            reliability_summary(run_reliability_corpus())
        ).encode(),
        "adapters": run_adapter_conformance().model_dump_json().encode(),
    }
    card = build_proof_card(reports)
    assert card["checks"]["reliability"]["status"] == "passed"
    assert card["checks"]["adapters"]["status"] == "passed"


def test_card_cli_exclusive_and_validate_before_output(tmp_path):
    source, output = tmp_path / "report.json", tmp_path / "card.md"
    source.write_text(json.dumps(report()), encoding="utf-8")
    args = SimpleNamespace(
        reliability=str(source),
        adapters=None,
        bundle=None,
        authority=None,
        output=str(output),
        format="markdown",
    )
    run_proof_card(args)
    initial = output.read_bytes()
    with pytest.raises(ProofCardError):
        run_proof_card(args)
    assert output.read_bytes() == initial
    source.write_text("invalid SECRET", encoding="utf-8")
    args.output = str(tmp_path / "missing.md")
    with pytest.raises(ProofCardError, match="invalid report JSON"):
        run_proof_card(args)
    assert not (tmp_path / "missing.md").exists()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_openai_responses_with_mock_transport(asynchronous):
    import importlib

    openai = pytest.importorskip("openai")
    # Use the SDK's installed HTTP backend, without adding it to base deps.
    backend_name = openai.DefaultHttpxClient.__mro__[1].__module__.split(".")[0]
    backend = importlib.import_module(backend_name)
    requests = []
    response_data = {
        "id": "resp_PRIVATE",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "fixture",
        "output": [],
        "usage": {
            "input_tokens": 2,
            "output_tokens": 3,
            "total_tokens": 5,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }

    def receive(request):
        requests.append(request)
        return backend.Response(200, json=response_data)

    sink, run_id = Sink(), generate_uuidv7()
    if asynchronous:

        async def execute():
            async with openai.AsyncOpenAI(
                api_key="fixture-not-real",
                max_retries=0,
                http_client=backend.AsyncClient(
                    transport=backend.MockTransport(receive)
                ),
            ) as client:
                wrap_responses_client(sink, client, lambda: run_id)
                return await client.responses.create(model="fixture", input="PRIVATE")

        result = asyncio.run(execute())
    else:
        with openai.OpenAI(
            api_key="fixture-not-real",
            max_retries=0,
            http_client=backend.Client(transport=backend.MockTransport(receive)),
        ) as client:
            wrap_responses_client(sink, client, lambda: run_id)
            result = client.responses.create(model="fixture", input="PRIVATE")
    assert result.id == "resp_PRIVATE"
    assert requests[0].headers["x-correlation-id"] == run_id
    assert sink.events[0]["completion_tokens"] == 3
    assert "PRIVATE" not in str(sink.events)


def test_metadata_sink_is_called_outside_state_lock():
    sink = Sink()
    callback = handler(sink)
    original = sink.record_event

    def reentrant(**kwargs):
        assert callback.stats()["pending"] == 0
        original(**kwargs)

    sink.record_event = reentrant
    callback.on_tool_start({}, "PRIVATE", run_id="run")
    callback.on_tool_end("PRIVATE", run_id="run")
    assert len(sink.events) == 1


def test_responses_invalid_metadata_never_replaces_success(caplog):
    response = {
        "id": "\ud800",
        "status": "PRIVATE",
        "usage": {"input_tokens": True, "output_tokens": -1},
    }
    client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: response)
    )
    wrap_responses_client(Sink(), client, generate_uuidv7)
    assert client.responses.create() is response
    assert "PRIVATE" not in caplog.text


def test_card_actual_bundle_verification_result(tmp_path):
    from dataclasses import asdict

    from test_evidence_bundle import _create_bundle

    from llmwitness.evidence_bundle import verify_evidence_bundle

    bundle, _ = _create_bundle(tmp_path)
    result = verify_evidence_bundle(bundle)
    assert result.valid
    data = json.dumps(asdict(result)).encode()
    card = build_proof_card({"bundle": data})
    assert card["checks"]["bundle"]["status"] == "passed"
    assert card["checks"]["bundle"]["entries_checked"] == 2
    assert result.run_id not in str(card)


@pytest.mark.parametrize(
    "summary",
    [
        {"valid": "true", "entries_checked": 1},
        {"valid": True, "entries_checked": 0},
        {"valid": False, "entries_checked": True},
    ],
)
def test_card_invalid_bundle_summaries(summary):
    with pytest.raises(ProofCardError):
        build_proof_card({"bundle": json.dumps(summary).encode()})
