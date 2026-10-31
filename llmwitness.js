/**
 * LLMWitness Headless-Safe JS SDK Bridge (llmwitness.js)
 * Zero-dependency Node.js and Browser JavaScript SDK for Playwright / Puppeteer script injection.
 * Intercepts fetch calls & DOM events, appends X-LLMWitness-Correlation-ID (UUIDv7),
 * and streams telemetry asynchronously via a debounced queue.
 */

(function () {
  const CORRELATION_HEADER = 'X-LLMWitness-Correlation-ID';
  const CORRELATION_ATTRIBUTE = 'data-llmwitness-correlation-id';
  const CORRELATION_EVENT = 'llmwitness:correlation-id';
  const UUIDV7_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

  function normalizeUUIDv7(value) {
    if (typeof value !== 'string' || !UUIDV7_PATTERN.test(value)) {
      throw new TypeError('correlationId must be an RFC 9562 UUIDv7');
    }
    return value.toLowerCase();
  }

  function readSharedCorrelationId() {
    if (typeof window === 'undefined') return null;

    let storedId = null;
    try {
      storedId = window.sessionStorage.getItem(CORRELATION_HEADER);
    } catch (_) {}

    const rootId =
      typeof document !== 'undefined' && document.documentElement
        ? document.documentElement.getAttribute(CORRELATION_ATTRIBUTE)
        : null;
    const candidate = [rootId, storedId, window.__LLMWITNESS_CORRELATION_ID__].find(
      (value) => typeof value === 'string' && UUIDV7_PATTERN.test(value)
    );
    return candidate ? candidate.toLowerCase() : null;
  }

  function publishCorrelationId(cid) {
    if (typeof window === 'undefined') return;
    try {
      window.__LLMWITNESS_CORRELATION_ID__ = cid;
    } catch (_) {}
    try {
      window.sessionStorage.setItem(CORRELATION_HEADER, cid);
    } catch (_) {}
    if (typeof document === 'undefined') return;
    if (document.documentElement) {
      document.documentElement.setAttribute(CORRELATION_ATTRIBUTE, cid);
    }
    if (typeof CustomEvent === 'function') {
      document.dispatchEvent(
        new CustomEvent(CORRELATION_EVENT, { detail: { correlation_id: cid } })
      );
    }
  }

  /**
   * Generates an RFC 9562 compliant UUIDv7 identifier in JavaScript.
   */
  function generateUUIDv7() {
    const now = Date.now();
    const hexTime = now.toString(16).padStart(12, '0');
    const randomBytes = new Uint8Array(10);
    if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
      crypto.getRandomValues(randomBytes);
    } else {
      for (let i = 0; i < randomBytes.length; i += 1) {
        randomBytes[i] = Math.floor(Math.random() * 256);
      }
    }
    const randomHex = Array.from(randomBytes, (value) =>
      value.toString(16).padStart(2, '0')
    ).join('');
    const randA = (parseInt(randomHex.slice(0, 4), 16) & 0x0fff)
      .toString(16)
      .padStart(3, '0');
    const variant = (8 + (parseInt(randomHex.slice(4, 6), 16) & 0x03)).toString(16);
    const randB = randomHex.slice(5, 20).padEnd(15, '0');

    return `${hexTime.slice(0, 8)}-${hexTime.slice(8, 12)}-7${randA}-${variant}${randB.slice(
      0,
      3
    )}-${randB.slice(3)}`;
  }

  class LLMWitnessBridge {
    constructor(options = {}) {
      this.ingestionUrl = (options.ingestionUrl || 'http://localhost:8000').replace(/\/$/, '');
      this.correlationId = Object.prototype.hasOwnProperty.call(options, 'correlationId')
        ? normalizeUUIDv7(options.correlationId)
        : readSharedCorrelationId() || generateUUIDv7();
      this.ingestToken = null;
      if (Object.prototype.hasOwnProperty.call(options, 'ingestToken')) {
        this.setIngestToken(options.ingestToken);
      }
      this.debounceMs = options.debounceMs || 100;
      this.maxQueueSize = options.maxQueueSize || 1000;
      this.queue = [];
      this.droppedEvents = 0;
      this.deliveryFailures = 0;
      this.timer = null;
      this.initialized = false;
      this.origFetch = null;
    }

    /**
     * Initializes fetch interception and DOM event tracking.
     */
    init(options = {}) {
      if (options.ingestionUrl) this.ingestionUrl = options.ingestionUrl.replace(/\/$/, '');
      if (Object.prototype.hasOwnProperty.call(options, 'correlationId')) {
        this.setCorrelationId(options.correlationId);
      }
      if (Object.prototype.hasOwnProperty.call(options, 'ingestToken')) {
        this.setIngestToken(options.ingestToken);
      }
      if (options.debounceMs) this.debounceMs = options.debounceMs;

      publishCorrelationId(this.correlationId);
      if (this.initialized) return this;
      this.initialized = true;

      this.patchFetch();
      this.attachDOMListeners();
      return this;
    }

    setCorrelationId(cid) {
      this.correlationId = normalizeUUIDv7(cid);
      publishCorrelationId(this.correlationId);
      return this;
    }

    getCorrelationId() {
      return this.correlationId;
    }

    /**
     * Configure an in-memory localhost ingestion token. The token is used only
     * for the Authorization header and is never added to telemetry payloads.
     * Passing null clears it.
     */
    setIngestToken(token) {
      if (token === null || typeof token === 'undefined') {
        this.ingestToken = null;
        return this;
      }
      if (typeof token !== 'string' || token.length === 0 || /[\r\n]/.test(token)) {
        throw new TypeError('ingestToken must be a non-empty single-line string or null');
      }
      this.ingestToken = token;
      return this;
    }

    enqueue(event) {
      if (this.queue.length >= this.maxQueueSize) {
        this.droppedEvents += 1;
        return false;
      }
      const payload = {
        ...event,
        correlation_id: this.correlationId,
        timestamp: Date.now() / 1000,
        url:
          typeof window !== 'undefined'
            ? `${window.location.origin}${window.location.pathname}`
            : 'http://node.local',
      };
      this.queue.push(payload);
      this.scheduleFlush();
      return true;
    }

    scheduleFlush() {
      if (this.timer) clearTimeout(this.timer);
      this.timer = setTimeout(() => this.flush(), this.debounceMs);
    }

    async flush() {
      if (this.queue.length === 0) return;
      const items = [...this.queue];
      this.queue = [];

      for (const item of items) {
        try {
          const endpoint = `${this.ingestionUrl}/ingest/extension`;
          const rawFetch =
            this.origFetch || (typeof fetch === 'function' ? fetch.bind(globalThis) : null);
          if (!rawFetch) {
            throw new Error('fetch is unavailable for ingestion delivery');
          }
          const headers = { 'Content-Type': 'application/json' };
          if (this.ingestToken !== null) {
            headers.Authorization = `Bearer ${this.ingestToken}`;
          }
          const response = await rawFetch(endpoint, {
            method: 'POST',
            headers,
            body: JSON.stringify(item),
          });
          if (!response || !response.ok) {
            const status = response && response.status ? response.status : 'unknown';
            throw new Error(`ingestion returned HTTP ${status}`);
          }
        } catch (err) {
          this.deliveryFailures += 1;
          // Silent warning to prevent disrupting host application flow
          if (typeof console !== 'undefined' && console.warn) {
            console.warn('[LLMWitness Bridge Warning] Ingestion streaming error:', err);
          }
        }
      }
    }

    patchFetch() {
      if (typeof window === 'undefined' || !window.fetch) return;
      const self = this;
      this.origFetch = window.fetch;

      window.fetch = async function (input, init) {
        const targetUrl = typeof input === 'string' ? input : (input && input.url ? input.url : '');

        // Skip intercepting ingestion endpoint calls to avoid recursion loops
        if (targetUrl && targetUrl.includes('/ingest/')) {
          return self.origFetch.apply(this, arguments);
        }

        init = init || {};
        let headers;
        const inheritedHeaders =
          typeof Request !== 'undefined' && input instanceof Request ? input.headers : undefined;
        if (init.headers instanceof Headers) {
          headers = init.headers;
        } else if (Array.isArray(init.headers)) {
          headers = new Headers(init.headers);
        } else {
          headers = new Headers(init.headers || inheritedHeaders || {});
        }

        if (headers.has(CORRELATION_HEADER)) {
          const suppliedCorrelationId = headers.get(CORRELATION_HEADER);
          if (typeof suppliedCorrelationId === 'string' && UUIDV7_PATTERN.test(suppliedCorrelationId)) {
            self.setCorrelationId(suppliedCorrelationId);
          } else {
            headers.set(CORRELATION_HEADER, self.correlationId);
          }
        } else {
          headers.set(CORRELATION_HEADER, self.correlationId);
        }
        init.headers = headers;

        self.enqueue({
          event_type: 'fetch_request',
          element_id: null,
          dom_delta: {
            method: init.method || 'GET',
            url: targetUrl,
          },
        });

        const response = await self.origFetch.call(this, input, init);
        if (response && response.headers && typeof response.headers.get === 'function') {
          const responseCorrelationId = response.headers.get(CORRELATION_HEADER);
          if (
            typeof responseCorrelationId === 'string' &&
            UUIDV7_PATTERN.test(responseCorrelationId)
          ) {
            self.setCorrelationId(responseCorrelationId);
          }
        }
        return response;
      };
    }

    attachDOMListeners() {
      if (typeof window === 'undefined' || typeof document === 'undefined') return;
      const self = this;

      ['click', 'submit', 'input'].forEach((eventType) => {
        document.addEventListener(
          eventType,
          (e) => {
            const target = e.target;
            const elemId = target ? target.id || target.name || target.tagName : null;
            self.enqueue({
              event_type: `dom_${eventType}`,
              element_id: elemId,
              dom_delta: {
                tagName: target ? target.tagName : null,
                value: eventType === 'input' ? '[REDACTED_INPUT]' : undefined,
              },
            });
          },
          true
        );
      });

      if (typeof MutationObserver !== 'undefined' && document.body) {
        const observer = new MutationObserver((mutations) => {
          let addedCount = 0;
          let removedCount = 0;
          mutations.forEach((m) => {
            addedCount += m.addedNodes.length;
            removedCount += m.removedNodes.length;
          });

          if (addedCount > 0 || removedCount > 0) {
            self.enqueue({
              event_type: 'dom_mutation',
              element_id: 'body',
              dom_delta: {
                type: 'childList',
                addedCount,
                removedCount,
              },
            });
          }
        });

        observer.observe(document.body, { childList: true, subtree: true });
      }
    }
  }

  const defaultBridge = new LLMWitnessBridge();

  if (typeof window !== 'undefined') {
    window.LLMWitness = defaultBridge;
    if (window.__LLMWITNESS_AUTO_INIT__ !== false) {
      defaultBridge.init();
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      LLMWitness: defaultBridge,
      LLMWitnessBridge,
      generateUUIDv7,
    };
  }
})();
