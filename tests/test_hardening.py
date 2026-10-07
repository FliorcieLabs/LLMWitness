"""Regression tests for scrubbing cost, local file permissions and consent enforcement."""

import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from llmwitness import keys
from llmwitness.integrations import make_langchain_handler
from llmwitness.sdk import LLMWitnessTracker
from llmwitness.session_store import SessionStore
from llmwitness.spool import EventSpool, classify_delivery, is_disabled
from llmwitness.utils import generate_uuidv7, normalize_uuidv7, redact_payload

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "hostile",
    [
        "a." * 50_000,
        "123.456." * 12_000,
        "x@" + "a." * 50_000,
        "a.b@" * 25_000,
        "a." * 50_000 + "@x.com",
        "a" * 100_000 + "@example.com",
        "1 " * 50_000,
        "+1 23 " * 16_000,
        "sk-" * 30_000,
    ],
)
def test_scrubbing_stays_fast_on_hostile_text(hostile):
    """Model output is untrusted: no pattern may backtrack quadratically on it."""
    started = time.perf_counter()
    redact_payload(hostile)
    assert time.perf_counter() - started < 2.0


def test_long_local_part_and_deep_subdomains_are_still_redacted():
    assert redact_payload("a" * 200 + "@example.com").endswith("[REDACTED_EMAIL]")
    assert redact_payload("me@a.b.c.d.mail.example.com") == "[REDACTED_EMAIL]"
    assert redact_payload("not-an-email@localhost") == "not-an-email@localhost"


def test_uuidv7_normalisation_and_delivery_classification():
    value = generate_uuidv7()
    assert normalize_uuidv7(value.upper()) == value
    for bad in ("not-a-uuid", "00000000-0000-4000-8000-000000000000", None, 7):
        with pytest.raises(ValueError):
            normalize_uuidv7(bad)
    assert [
        classify_delivery(code) for code in (200, 201, 400, 409, 413, 429, 500, 503)
    ] == [
        "delivered",
        "delivered",
        "discard",
        "discard",
        "discard",
        "retry",
        "retry",
        "retry",
    ]
    assert is_disabled(" OFF ") and is_disabled("") and not is_disabled("spool")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_local_state_files_are_private_to_the_owner(tmp_path):
    store = SessionStore(tmp_path / "state" / "sessions.db")
    store.close()
    assert stat.S_IMODE((tmp_path / "state" / "sessions.db").stat().st_mode) == 0o600

    spool = EventSpool(tmp_path / "spool")
    assert spool.append("sdk", {"n": 1})
    assert stat.S_IMODE((tmp_path / "spool").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "spool" / "sdk.jsonl").stat().st_mode) == 0o600

    identity = keys.create_identity(tmp_path / "keys")
    assert stat.S_IMODE(identity.directory.stat().st_mode) == 0o700
    assert (
        stat.S_IMODE((identity.directory / keys.HMAC_KEY_FILE).stat().st_mode) == 0o600
    )


def test_incomplete_key_directory_is_reported_not_overwritten(tmp_path):
    identity = keys.create_identity(tmp_path)
    private_pem = (tmp_path / keys.PRIVATE_KEY_FILE).read_text(encoding="utf-8")
    (tmp_path / keys.HMAC_KEY_FILE).unlink()
    with pytest.raises(RuntimeError, match="incomplete key set"):
        keys.load_or_create_identity(tmp_path)
    assert (tmp_path / keys.PRIVATE_KEY_FILE).read_text(encoding="utf-8") == private_pem
    assert identity.fingerprint == keys.local_trusted_fingerprint(tmp_path)


def test_langchain_handler_with_the_real_library(monkeypatch, tmp_path):
    pytest.importorskip("langchain_core")
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.tools import tool

    tracker = LLMWitnessTracker(
        ingestion_url="http://ingest.invalid", spool_dir=tmp_path
    )
    delivered: list[dict] = []

    class _Accepted:
        status_code = 201

    monkeypatch.setattr(
        tracker.http_client,
        "post",
        lambda url, **kwargs: delivered.append(kwargs["json"]) or _Accepted(),
    )

    @tool
    def add_one(number: int) -> int:
        """Add one to a number."""
        return number + 1

    handler = make_langchain_handler(tracker)
    config = {"callbacks": [handler]}
    with tracker.trace_session("langchain-run") as correlation_id:
        reply = FakeListChatModel(responses=["the answer"]).invoke(
            "what is it?", config=config
        )
        assert add_one.invoke({"number": 1}, config=config) == 2
    tracker.shutdown()

    assert reply.content == "the answer"
    model_event, tool_event = delivered
    assert {event["correlation_id"] for event in delivered} == {correlation_id}
    assert model_event["provider"] == "langchain"
    assert model_event["completion_string"] == "the answer"
    assert model_event["input_messages"][-1]["content"] == "what is it?"
    assert tool_event["tool_calls"][0]["function"]["name"] == "add_one"
    assert tool_event["completion_string"] == "2"


_CONSENT_SCRIPT = r"""
const path = process.argv[1];
const cid = '019fd93b-a06b-7799-947f-67f80b9edc04';
let listener = null;
let fetchCalls = 0;
let allowed = ['http://localhost:3000'];
global.chrome = {
  runtime: {lastError: null, onMessage: {addListener: (cb) => { listener = cb; }}},
  storage: {
    session: {get: (keys, cb) => cb({})},
    local: {get: (keys, cb) => cb({llmwitnessAllowedOrigins: allowed})},
  },
};
global.fetch = async () => { fetchCalls += 1; return {ok: true, status: 201, json: async () => ({})}; };
require(path);
const payload = {
  correlation_id: cid, timestamp: 1, url: 'http://localhost:3000/',
  event_type: 'click', element_id: null, dom_delta: {},
};
const ask = (message, sender) => new Promise((resolve, reject) => {
  if (listener(message, sender, resolve) !== true) reject(new Error('listener closed early'));
  setTimeout(() => reject(new Error('timed out')), 1000);
});
(async () => {
  const telemetry = {action: 'LLMWITNESS_DOM_TELEMETRY', payload};
  const query = {action: 'LLMWITNESS_CONSENT_QUERY'};
  if ((await ask(query, {origin: 'http://localhost:3000'})).allowed !== true) process.exit(2);
  if ((await ask(query, {url: 'http://localhost:3000/page?x=1'})).allowed !== true) process.exit(3);
  if ((await ask(query, {origin: 'http://localhost:4000'})).allowed !== false) process.exit(4);
  if ((await ask(query, {})).allowed !== false) process.exit(5);

  if ((await ask(telemetry, {origin: 'http://localhost:3000'})).status !== 'success') process.exit(6);
  if (fetchCalls !== 1) process.exit(7);
  const denied = await ask(telemetry, {origin: 'http://localhost:4000'});
  if (denied.status !== 'error' || fetchCalls !== 1) process.exit(8);

  allowed = [];
  const revoked = await ask(telemetry, {origin: 'http://localhost:3000'});
  if (revoked.status !== 'error' || fetchCalls !== 1) process.exit(9);
})().catch((error) => { console.error(error); process.exit(10); });
"""


def test_extension_worker_enforces_the_per_site_allow_list():
    """The worker must refuse telemetry from a site the user has not allowed."""
    try:
        result = subprocess.run(
            ["node", "-e", _CONSENT_SCRIPT, str(ROOT / "extension" / "background.js")],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        pytest.skip("Node.js binary not installed on runner path")
    assert result.returncode == 0, result.stderr


def test_content_script_captures_nothing_before_consent():
    content = (ROOT / "extension" / "content.js").read_text(encoding="utf-8")
    send = content.index("function sendTelemetry(")
    assert "if (!capturing) return;" in content[send : send + 200]
    # Observation starts only inside startCapture, never at load time.
    assert content.count("observer.observe(") == 1
    assert content.index("function startCapture()") < content.index("observer.observe(")
    assert "LLMWITNESS_CONSENT_QUERY" in content
    popup = (ROOT / "extension" / "popup.js").read_text(encoding="utf-8")
    assert "innerHTML" not in popup


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def test_serve_command_runs_both_services_and_wires_them_together(tmp_path):
    ingest_port, gateway_port = _free_port(), _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "LLMWITNESS_MOCK_UPSTREAM": "true",
        "LLMWITNESS_SESSION_DB": str(tmp_path / "sessions.db"),
        "LLMWITNESS_KEY_DIR": str(tmp_path / "keys"),
        "LLMWITNESS_RECEIPT_DIR": str(tmp_path / "receipts"),
        "LLMWITNESS_SPOOL_DIR": str(tmp_path / "spool"),
        "LLMWITNESS_EPHEMERAL_KEYS": "0",
    }
    env.pop("INGESTION_SERVER_URL", None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "llmwitness.cli",
            "serve",
            "--ingest-port",
            str(ingest_port),
            "--gateway-port",
            str(gateway_port),
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ingest_url = f"http://127.0.0.1:{ingest_port}"
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    try:
        with httpx.Client(timeout=2.0) as client:
            for _ in range(60):
                try:
                    if (
                        client.get(f"{ingest_url}/health").status_code == 200
                        and client.get(f"{gateway_url}/health").status_code == 200
                    ):
                        break
                except httpx.HTTPError:
                    time.sleep(0.2)
            else:
                pytest.fail("llmwitness serve did not start both services")

            correlation_id = generate_uuidv7()
            proxied = client.post(
                f"{gateway_url}/v1/chat/completions",
                json={"model": "m", "messages": []},
                headers={"X-LLMWitness-Correlation-ID": correlation_id},
            )
            assert proxied.status_code == 200
            # The gateway must report to the ingestion port chosen on the command line.
            for _ in range(30):
                session = client.get(f"{ingest_url}/ingest/session/{correlation_id}")
                if session.status_code == 200:
                    break
                time.sleep(0.1)
            assert session.status_code == 200
            assert len(session.json()["gateway_events"]) == 1
            assert client.get(f"{ingest_url}/health").json()["durable_sessions"] is True
            sealed = client.post(
                f"{ingest_url}/ingest/seal", json={"correlation_id": correlation_id}
            )
            assert sealed.status_code == 200
        assert (tmp_path / "keys" / keys.PRIVATE_KEY_FILE).is_file()
        assert (tmp_path / "receipts" / f"{correlation_id}.json").is_file()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
