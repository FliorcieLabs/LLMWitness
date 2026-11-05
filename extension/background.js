/**
 * LLMWitness opt-in localhost background worker (Manifest V3).
 */

const INGESTION_ENDPOINT = "http://127.0.0.1:8000/ingest/extension";
const INGEST_TOKEN_STORAGE_KEY = 'llmwitnessIngestToken';
const ALLOWED_ORIGINS_STORAGE_KEY = 'llmwitnessAllowedOrigins';
const UUIDV7_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function senderOrigin(sender) {
  if (sender && typeof sender.origin === 'string' && sender.origin) return sender.origin;
  try {
    return new URL(sender.url).origin;
  } catch (_) {
    return null;
  }
}

// Capture is off for every site until the user allows it from the popup. The
// worker re-checks the allow-list so a content script cannot bypass consent.
function isOriginAllowed(sender) {
  return new Promise((resolve) => {
    if (!chrome.storage || !chrome.storage.local) {
      // No persistent storage to hold a consent decision (non-extension host).
      resolve(true);
      return;
    }
    const origin = senderOrigin(sender);
    if (!origin) {
      resolve(false);
      return;
    }
    try {
      chrome.storage.local.get([ALLOWED_ORIGINS_STORAGE_KEY], (stored) => {
        if (chrome.runtime.lastError) {
          resolve(false);
          return;
        }
        const allowed = stored && stored[ALLOWED_ORIGINS_STORAGE_KEY];
        resolve(Array.isArray(allowed) && allowed.includes(origin));
      });
    } catch (_) {
      resolve(false);
    }
  });
}

function buildIngestionHeaders() {
  return new Promise((resolve, reject) => {
    // storage.session is non-persistent and, by default, available only to
    // trusted extension contexts such as this service worker.
    if (!chrome.storage || !chrome.storage.session) {
      reject(new Error('chrome.storage.session is unavailable'));
      return;
    }

    try {
      chrome.storage.session.get([INGEST_TOKEN_STORAGE_KEY], (stored) => {
        if (chrome.runtime.lastError) {
          reject(new Error('Unable to read the in-memory ingest token'));
          return;
        }

        const token = stored && stored[INGEST_TOKEN_STORAGE_KEY];
        if (token !== undefined && token !== null &&
            (typeof token !== 'string' || token.length === 0 || /[\r\n]/.test(token))) {
          reject(new Error('The in-memory ingest token is invalid'));
          return;
        }

        const headers = { 'Content-Type': 'application/json' };
        if (typeof token === 'string') headers.Authorization = `Bearer ${token}`;
        resolve(headers);
      });
    } catch (_) {
      reject(new Error('Unable to read the in-memory ingest token'));
    }
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.action === 'LLMWITNESS_CONSENT_QUERY') {
    isOriginAllowed(sender).then(allowed => sendResponse({ allowed: allowed }));
    return true;
  }

  if (message && message.action === 'LLMWITNESS_DOM_TELEMETRY') {
    const payload = message.payload;

    if (!payload || !UUIDV7_PATTERN.test(payload.correlation_id || '')) {
      sendResponse({ status: 'error', error: 'correlation_id must be an RFC 9562 UUIDv7' });
      return false;
    }

    isOriginAllowed(sender)
    .then(allowed => {
      if (!allowed) throw new Error('capture is not allowed for this site');
      return buildIngestionHeaders();
    })
    .then(headers => fetch(INGESTION_ENDPOINT, {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(payload)
    }))
    .then(response => {
      if (!response.ok) throw new Error(`ingestion returned HTTP ${response.status}`);
      return response.json();
    })
    .then(data => {
      sendResponse({ status: 'success', data: data });
    })
    .catch(error => {
      sendResponse({ status: 'error', error: error.message });
    });

    // Return true to indicate asynchronous response handling
    return true;
  }
});
