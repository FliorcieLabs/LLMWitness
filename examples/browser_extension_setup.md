# Browser extension setup

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Load the `extension/` folder as an unpacked extension.
4. Start the local ingestion service and gateway.
5. Use the browser-side telemetry bridge for local development only.

The extension is optional and intended for local inspection of agent sessions.

If the ingestion service uses `LLMWITNESS_INGEST_TOKEN`, open the extension's
service-worker inspector from `chrome://extensions` and set the matching token
for the current browser session:

```javascript
chrome.storage.session.set({ llmwitnessIngestToken: 'your-local-token' })
```

Clear it with
`chrome.storage.session.remove('llmwitnessIngestToken')`. The extension keeps
this value in non-persistent extension session storage and uses it only for the
ingestion request's `Authorization` header.

For the page bridge, configure the same in-memory token after loading
`llmwitness.js`:

```javascript
window.LLMWitness.setIngestToken('your-local-token')
```

Passing `null` clears the page bridge token. Do not embed a real token in a
checked-in script or expose either localhost service to an untrusted network.

