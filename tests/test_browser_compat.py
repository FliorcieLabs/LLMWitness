"""
LLMWitness Community - Browser Compatibility Suite
Validates JavaScript SDK export structure, UUIDv7 generation in browser engine, and Chrome Extension manifest schema.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_js_sdk_file_structure():
    js_sdk_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "llmwitness.js")
    )
    assert os.path.exists(js_sdk_path), "llmwitness.js missing from workspace root"

    with open(js_sdk_path, encoding="utf-8") as f:
        code = f.read()

    assert (
        "LLMWitnessBrowserSDK" in code
        or "class LLMWitness" in code
        or "recordMutation" in code
        or "uuidv7" in code
    )


def test_chrome_extension_manifest_v3_schema():
    manifest_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "extension", "manifest.json")
    )
    assert os.path.exists(manifest_path), "Chrome extension manifest.json missing"

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    assert manifest.get("manifest_version") == 3
    assert "name" in manifest
    assert "version" in manifest
    assert "content_scripts" in manifest or "background" in manifest


def test_extension_sidecar_uses_shared_uuidv7_contract():
    """The MV3 sidecar must emit IDs the gateway can use for session linking."""
    content_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "extension", "content.js")
    )
    with open(content_path, encoding="utf-8") as f:
        code = f.read()

    assert "X-LLMWitness-Correlation-ID" in code
    assert "data-llmwitness-correlation-id" in code
    assert "llmwitness:correlation-id" in code
    assert "UUIDV7_PATTERN" in code
    assert "correlation_id: getActiveCorrelationId()" in code


def test_browser_bridge_bounds_queue_and_omits_url_query_strings():
    js_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "llmwitness.js")
    )
    with open(js_path, encoding="utf-8") as f:
        code = f.read()

    assert "maxQueueSize" in code
    assert "droppedEvents" in code
    assert "window.location.origin" in code
    assert "window.location.pathname" in code
    assert "window.location.href" not in code
    assert "input instanceof Request ? input.headers" in code


def test_js_sdk_node_execution():
    """Runs node syntax validation on llmwitness.js if node binary exists."""
    js_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "llmwitness.js")
    )
    try:
        res = subprocess.run(["node", "-c", js_path], capture_output=True, text=True)
        if res.returncode != 0:
            pytest.fail(f"JavaScript SDK syntax check failed: {res.stderr}")
    except FileNotFoundError:
        pytest.skip("Node.js binary not installed on runner path")


def test_js_sdk_runtime_uuid_and_queue_contract():
    """Exercise exported browser-bridge behavior in Node when available."""
    js_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "llmwitness.js")
    )
    script = r"""
const sdk = require(process.argv[1]);
const pattern = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
for (let index = 0; index < 1000; index += 1) {
  if (!pattern.test(sdk.generateUUIDv7())) process.exit(2);
}
const bridge = new sdk.LLMWitnessBridge({maxQueueSize: 2, debounceMs: 60000});
bridge.enqueue({event_type: 'one', dom_delta: {}});
bridge.enqueue({event_type: 'two', dom_delta: {}});
bridge.enqueue({event_type: 'three', dom_delta: {}});
if (bridge.queue.length !== 2 || bridge.droppedEvents !== 1) process.exit(3);
clearTimeout(bridge.timer);
"""
    try:
        result = subprocess.run(
            ["node", "-e", script, js_path], capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        pytest.skip("Node.js binary not installed on runner path")
    assert result.returncode == 0, result.stderr


def test_js_sdk_rejects_invalid_correlation_ids_at_public_boundaries():
    """Constructor, init, and setter must enforce the shared UUIDv7 contract."""
    js_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "llmwitness.js")
    )
    script = r"""
const sdk = require(process.argv[1]);
const valid = '019fd93b-a06b-7799-947f-67f80b9edc04';
const invalid = ['not-a-uuid', '', null, '550e8400-e29b-41d4-a716-446655440000'];
function mustThrow(fn) {
  let threw = false;
  try { fn(); } catch (error) { threw = /RFC 9562 UUIDv7/.test(error.message); }
  if (!threw) process.exit(2);
}
for (const cid of invalid) mustThrow(() => new sdk.LLMWitnessBridge({correlationId: cid}));
const bridge = new sdk.LLMWitnessBridge({correlationId: valid.toUpperCase(), debounceMs: 60000});
if (bridge.getCorrelationId() !== valid) process.exit(3);
mustThrow(() => bridge.setCorrelationId('550e8400-e29b-41d4-a716-446655440000'));
if (bridge.getCorrelationId() !== valid) process.exit(4);
mustThrow(() => bridge.init({correlationId: 'wrong'}));
if (bridge.getCorrelationId() !== valid) process.exit(5);
bridge.enqueue({correlation_id: 'caller-cannot-override', event_type: 'test', dom_delta: {}});
if (bridge.queue[0].correlation_id !== valid) process.exit(6);
clearTimeout(bridge.timer);
"""
    try:
        result = subprocess.run(
            ["node", "-e", script, js_path], capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        pytest.skip("Node.js binary not installed on runner path")
    assert result.returncode == 0, result.stderr


def test_js_sdk_fetch_interception_auth_and_correlation_handoff():
    """Exercise fetch patching, auth delivery, and gateway response handoff."""
    js_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "llmwitness.js")
    )
    script = r"""
const sdk = require(process.argv[1]);
const initialCid = '019fd93b-a06b-7799-947f-67f80b9edc04';
const responseCid = '019fd93b-a06b-7799-a47f-67f80b9edc04';
const token = 'local-browser-ingest-token';
const calls = [];

class MockHeaders {
  constructor(init = {}) {
    this.values = new Map();
    if (init instanceof MockHeaders) {
      for (const [key, value] of init.values) this.set(key, value);
    } else if (Array.isArray(init)) {
      for (const [key, value] of init) this.set(key, value);
    } else {
      for (const [key, value] of Object.entries(init)) this.set(key, value);
    }
  }
  has(key) { return this.values.has(key.toLowerCase()); }
  get(key) { return this.values.get(key.toLowerCase()) || null; }
  set(key, value) { this.values.set(key.toLowerCase(), String(value)); }
}

const attributes = new Map();
const session = new Map();
global.Headers = MockHeaders;
global.Request = class MockRequest {};
global.CustomEvent = class CustomEvent {
  constructor(type, init) { this.type = type; this.detail = init.detail; }
};
global.document = {
  body: null,
  documentElement: {
    getAttribute: (key) => attributes.get(key) || null,
    setAttribute: (key, value) => attributes.set(key, value),
  },
  addEventListener: () => {},
  dispatchEvent: () => true,
};
global.window = {
  location: {origin: 'http://localhost:3000', pathname: '/agent'},
  sessionStorage: {
    getItem: (key) => session.get(key) || null,
    setItem: (key, value) => session.set(key, value),
  },
  fetch: async (input, init) => {
    calls.push({input, init});
    return {
      ok: true,
      status: 201,
      headers: new MockHeaders({'X-LLMWitness-Correlation-ID': responseCid}),
    };
  },
};

(async () => {
  const bridge = new sdk.LLMWitnessBridge({
    correlationId: initialCid,
    ingestToken: token,
    debounceMs: 60000,
  });
  bridge.init();
  await window.fetch('http://model.local/v1/chat', {method: 'POST'});
  if (calls[0].init.headers.get('X-LLMWitness-Correlation-ID') !== initialCid) process.exit(2);
  if (bridge.queue[0].correlation_id !== initialCid) process.exit(3);
  if (bridge.getCorrelationId() !== responseCid) process.exit(4);
  if (attributes.get('data-llmwitness-correlation-id') !== responseCid) process.exit(5);

  clearTimeout(bridge.timer);
  await bridge.flush();
  const delivery = calls.find((call) => call.input.endsWith('/ingest/extension'));
  if (!delivery) process.exit(6);
  if (delivery.init.headers.Authorization !== `Bearer ${token}`) process.exit(7);
  if (delivery.init.body.includes(token)) process.exit(8);
  if (bridge.deliveryFailures !== 0) process.exit(9);
})().catch((error) => { console.error(error); process.exit(10); });
"""
    try:
        result = subprocess.run(
            ["node", "-e", script, js_path], capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        pytest.skip("Node.js binary not installed on runner path")
    assert result.returncode == 0, result.stderr


def test_js_sdk_counts_non_2xx_delivery_failures():
    js_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "llmwitness.js")
    )
    script = r"""
const sdk = require(process.argv[1]);
global.fetch = async () => ({ok: false, status: 503});
console.warn = () => {};
(async () => {
  const bridge = new sdk.LLMWitnessBridge({debounceMs: 60000});
  bridge.enqueue({event_type: 'delivery-test', dom_delta: {}});
  clearTimeout(bridge.timer);
  await bridge.flush();
  if (bridge.deliveryFailures !== 1 || bridge.queue.length !== 0) process.exit(2);
})().catch((error) => { console.error(error); process.exit(3); });
"""
    try:
        result = subprocess.run(
            ["node", "-e", script, js_path], capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        pytest.skip("Node.js binary not installed on runner path")
    assert result.returncode == 0, result.stderr


def test_extension_worker_uses_session_token_without_capturing_it():
    """The MV3 worker keeps auth in session storage and out of event bodies."""
    background_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "extension", "background.js")
    )
    script = r"""
const path = process.argv[1];
const token = 'local-extension-ingest-token';
const cid = '019fd93b-a06b-7799-947f-67f80b9edc04';
let listener = null;
let fetchCall = null;
global.chrome = {
  runtime: {
    lastError: null,
    onMessage: {addListener: (callback) => { listener = callback; }},
  },
  storage: {
    session: {
      get: (keys, callback) => callback({llmwitnessIngestToken: token}),
    },
  },
};
global.fetch = async (url, init) => {
  fetchCall = {url, init};
  return {ok: true, status: 201, json: async () => ({accepted: true})};
};
require(path);
if (typeof listener !== 'function') process.exit(2);

const payload = {
  correlation_id: cid,
  timestamp: 1,
  url: 'http://localhost/agent',
  event_type: 'click',
  element_id: null,
  dom_delta: {},
};

(async () => {
  const response = await new Promise((resolve, reject) => {
    const keepAlive = listener(
      {action: 'LLMWITNESS_DOM_TELEMETRY', payload},
      {},
      resolve
    );
    if (keepAlive !== true) reject(new Error('listener did not remain active'));
    setTimeout(() => reject(new Error('listener timed out')), 1000);
  });
  if (response.status !== 'success') process.exit(3);
  if (fetchCall.init.headers.Authorization !== `Bearer ${token}`) process.exit(4);
  if (fetchCall.init.body.includes(token)) process.exit(5);

  let invalidResponse = null;
  const keepAlive = listener(
    {action: 'LLMWITNESS_DOM_TELEMETRY', payload: {...payload, correlation_id: 'bad'}},
    {},
    (value) => { invalidResponse = value; }
  );
  if (keepAlive !== false || invalidResponse.status !== 'error') process.exit(6);
})().catch((error) => { console.error(error); process.exit(7); });
"""
    try:
        result = subprocess.run(
            ["node", "-e", script, background_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        pytest.skip("Node.js binary not installed on runner path")
    assert result.returncode == 0, result.stderr


def test_extension_content_script_node_syntax():
    """Runs syntax validation on the MV3 content script when Node is available."""
    content_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "extension", "content.js")
    )
    try:
        res = subprocess.run(
            ["node", "--check", content_path], capture_output=True, text=True
        )
        if res.returncode != 0:
            pytest.fail(f"Extension content script syntax check failed: {res.stderr}")
    except FileNotFoundError:
        pytest.skip("Node.js binary not installed on runner path")
