/**
 * LLMWitness opt-in localhost content script (Manifest V3).
 * Captures structural DOM mutations and user interactions correlated by X-LLMWitness-Correlation-ID.
 * Includes adaptive throttling & noise filtering for high-frequency React/Vue SPA re-renders.
 *
 * Nothing is observed until the user allows this site from the extension popup,
 * and a visible indicator stays on the page for as long as capture is active.
 */

(function () {
  const CORRELATION_HEADER = 'X-LLMWitness-Correlation-ID';
  const CORRELATION_ATTRIBUTE = 'data-llmwitness-correlation-id';
  const CORRELATION_EVENT = 'llmwitness:correlation-id';
  const UUIDV7_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const ALLOWED_ORIGINS_STORAGE_KEY = 'llmwitnessAllowedOrigins';
  const INDICATOR_ID = 'llmwitness-capture-indicator';
  let activeCorrelationId = null;
  let capturing = false;

  function safePageUrl() {
    return `${window.location.origin}${window.location.pathname}`;
  }

  // Utility: Generate RFC 9562 compliant UUIDv7 in browser JS
  function generateUUIDv7() {
    const ms = Date.now();
    const msHex = ms.toString(16).padStart(12, '0');

    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);

    for (let i = 0; i < 6; i++) {
      bytes[i] = parseInt(msHex.slice(i * 2, i * 2 + 2), 16);
    }

    bytes[6] = (bytes[6] & 0x0f) | 0x70;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;

    const hex = Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function isUUIDv7(value) {
    return typeof value === 'string' && UUIDV7_PATTERN.test(value);
  }

  function publishCorrelationId(cid) {
    activeCorrelationId = cid;

    // sessionStorage and the root attribute are visible to both the page and the
    // isolated content-script world. The window property is retained for pages
    // that inject this script directly rather than through Manifest V3.
    try { sessionStorage.setItem(CORRELATION_HEADER, cid); } catch (_) {}
    try { window.__LLMWITNESS_CORRELATION_ID__ = cid; } catch (_) {}
    if (document.documentElement &&
        document.documentElement.getAttribute(CORRELATION_ATTRIBUTE) !== cid) {
      document.documentElement.setAttribute(CORRELATION_ATTRIBUTE, cid);
    }
  }

  function getActiveCorrelationId() {
    let storedId = null;
    try { storedId = sessionStorage.getItem(CORRELATION_HEADER); } catch (_) {}

    const candidates = [
      document.documentElement && document.documentElement.getAttribute(CORRELATION_ATTRIBUTE),
      storedId,
      window.__LLMWITNESS_CORRELATION_ID__,
      activeCorrelationId
    ];
    const cid = candidates.find(isUUIDv7) || generateUUIDv7();

    // Re-publish even when unchanged because documentElement may not have
    // existed during the document_start initialization.
    publishCorrelationId(cid);
    return cid;
  }

  // A page SDK can dispatch this event after adopting a correlation ID from a
  // gateway response. CustomEvent detail crosses the MV3 isolated-world boundary.
  document.addEventListener(CORRELATION_EVENT, function (event) {
    const cid = event && event.detail && event.detail.correlation_id;
    if (isUUIDv7(cid)) publishCorrelationId(cid);
  });

  function showIndicator() {
    if (!document.documentElement || document.getElementById(INDICATOR_ID)) return;
    const host = document.createElement('div');
    host.id = INDICATOR_ID;
    // A closed shadow root keeps page styles from hiding or restyling the notice.
    const root = host.attachShadow ? host.attachShadow({ mode: 'closed' }) : host;
    const badge = document.createElement('div');
    badge.textContent = '\u25CF LLMWitness is recording this page';
    badge.setAttribute('role', 'status');
    badge.style.cssText = [
      'position:fixed', 'right:12px', 'bottom:12px', 'z-index:2147483647',
      'padding:6px 10px', 'border-radius:6px', 'background:#8a1c1c', 'color:#fff',
      'font:12px/1.4 system-ui,sans-serif', 'pointer-events:none',
      'box-shadow:0 1px 4px rgba(0,0,0,.35)'
    ].join(';');
    root.appendChild(badge);
    document.documentElement.appendChild(host);
  }

  function hideIndicator() {
    const host = document.getElementById(INDICATOR_ID);
    if (host && host.parentNode) host.parentNode.removeChild(host);
  }

  function isIndicatorNode(node) {
    return !!node && node.id === INDICATOR_ID;
  }

  // Telemetry Throttling & Batching Queue
  let telemetryBuffer = [];
  let throttleTimer = null;
  const THROTTLE_MS = 150; // Batch mutations every 150ms to prevent worker thread congestion

  function flushTelemetryBuffer() {
    if (telemetryBuffer.length === 0) return;

    const batchPayload = {
      correlation_id: getActiveCorrelationId(),
      timestamp: Date.now() / 1000,
      url: safePageUrl(),
      event_type: 'dom_batch_mutation',
      element_id: 'document-root',
      dom_delta: {
        batchSize: telemetryBuffer.length,
        events: telemetryBuffer.slice(0, 20) // Cap top 20 significant events per batch
      }
    };

    telemetryBuffer = [];

    if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.sendMessage) {
      chrome.runtime.sendMessage({ action: 'LLMWITNESS_DOM_TELEMETRY', payload: batchPayload }, function (response) {
        if (chrome.runtime.lastError) {
          // Worker worker unreachable
        }
      });
    }
  }

  function sendTelemetry(eventType, elementId, domDelta) {
    if (!capturing) return;
    // Direct UI interactions (clicks, keypresses) send immediately
    if (eventType === 'click' || eventType === 'keydown' || eventType === 'input') {
      const immediatePayload = {
        correlation_id: getActiveCorrelationId(),
        timestamp: Date.now() / 1000,
        url: safePageUrl(),
        event_type: eventType,
        element_id: elementId || null,
        dom_delta: domDelta || {}
      };

      if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.sendMessage) {
        chrome.runtime.sendMessage({ action: 'LLMWITNESS_DOM_TELEMETRY', payload: immediatePayload });
      }
      return;
    }

    // Structural DOM mutations get buffered and throttled
    telemetryBuffer.push({ eventType, elementId, domDelta, ts: Date.now() });

    if (!throttleTimer) {
      throttleTimer = setTimeout(function () {
        throttleTimer = null;
        flushTelemetryBuffer();
      }, THROTTLE_MS);
    }
  }

  // 1. Setup DOM MutationObserver with Noise Filtering
  const observer = new MutationObserver(function (mutations) {
    mutations.forEach(function (mutation) {
      // Filter out non-structural noise (pure style, animation class changes)
      if (mutation.type === 'attributes' && (mutation.attributeName === 'style' || mutation.attributeName === 'class')) {
        return;
      }

      const addedNodes = Array.from(mutation.addedNodes)
        .filter(node => node.nodeType === 1 && !isIndicatorNode(node)) // Only element nodes
        .map(node => ({
          tagName: node.tagName || null,
          id: node.id || null
        }));

      const removedNodes = Array.from(mutation.removedNodes)
        .filter(node => node.nodeType === 1 && !isIndicatorNode(node))
        .map(node => ({
          tagName: node.tagName || null,
          id: node.id || null
        }));

      if (addedNodes.length > 0 || removedNodes.length > 0) {
        sendTelemetry('dom_mutation', mutation.target.id || mutation.target.tagName, {
          type: mutation.type,
          addedCount: addedNodes.length,
          removedCount: removedNodes.length,
          addedNodesSummary: addedNodes.slice(0, 3),
          removedNodesSummary: removedNodes.slice(0, 3)
        });
      }
    });
  });

  function startCapture() {
    if (capturing) return;
    capturing = true;
    // Establish the shared UUIDv7 before the first observed mutation or input.
    getActiveCorrelationId();
    showIndicator();
    if (document.documentElement) {
      observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributeFilter: ['id', 'data-llmwitness-id', 'href', 'type'] // Filter out noisy style/class attributes
      });
    }
  }

  function stopCapture() {
    if (!capturing) return;
    capturing = false;
    observer.disconnect();
    telemetryBuffer = [];
    hideIndicator();
  }

  function applyConsent(allowedOrigins) {
    const allowed = Array.isArray(allowedOrigins) &&
      allowedOrigins.includes(window.location.origin);
    if (allowed) startCapture(); else stopCapture();
  }

  const hasExtensionApis = typeof chrome !== 'undefined' && chrome.runtime &&
    chrome.runtime.sendMessage;
  if (hasExtensionApis) {
    chrome.runtime.sendMessage({ action: 'LLMWITNESS_CONSENT_QUERY' }, function (response) {
      if (chrome.runtime.lastError) return;
      if (response && response.allowed === true) startCapture();
    });
    // Allowing or removing the site in the popup takes effect without a reload.
    if (chrome.storage && chrome.storage.onChanged) {
      chrome.storage.onChanged.addListener(function (changes, area) {
        if (area === 'local' && changes[ALLOWED_ORIGINS_STORAGE_KEY]) {
          applyConsent(changes[ALLOWED_ORIGINS_STORAGE_KEY].newValue);
        }
      });
    }
  }

  // 2. Click Event Listener
  document.addEventListener('click', function (e) {
    const target = e.target;
    sendTelemetry('click', target.id || null, {
      tagName: target.tagName,
      className: target.className,
      x: e.clientX,
      y: e.clientY
    });
  }, true);

  // 3. Input Event Listener
  document.addEventListener('input', function (e) {
    const target = e.target;
    sendTelemetry('input', target.id || null, {
      tagName: target.tagName,
      type: target.type || null,
      valueLength: (target.value || '').length
    });
  }, true);

  // 4. Keydown Event Listener
  document.addEventListener('keydown', function (e) {
    const target = e.target;
    sendTelemetry('keydown', target.id || null, {
      key: e.key.length === 1 ? '[CHAR]' : e.key,
      code: e.code,
      ctrlKey: e.ctrlKey,
      shiftKey: e.shiftKey
    });
  }, true);

})();
