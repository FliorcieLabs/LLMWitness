/**
 * LLMWitness popup: the per-site allow-list that gates all capture.
 */

const ALLOWED_ORIGINS_STORAGE_KEY = 'llmwitnessAllowedOrigins';
const CAPTURABLE = /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;

function readAllowed(callback) {
  chrome.storage.local.get([ALLOWED_ORIGINS_STORAGE_KEY], (stored) => {
    const allowed = stored && stored[ALLOWED_ORIGINS_STORAGE_KEY];
    callback(Array.isArray(allowed) ? allowed : []);
  });
}

function writeAllowed(allowed, callback) {
  chrome.storage.local.set({ [ALLOWED_ORIGINS_STORAGE_KEY]: allowed }, callback);
}

function render(currentOrigin) {
  readAllowed((allowed) => {
    const current = document.getElementById('current');
    const toggle = document.getElementById('toggle');
    if (!currentOrigin || !CAPTURABLE.test(currentOrigin)) {
      current.textContent = 'This tab is not a localhost page, so it cannot be recorded.';
      toggle.hidden = true;
    } else {
      const isAllowed = allowed.includes(currentOrigin);
      current.textContent = isAllowed
        ? `Recording ${currentOrigin}`
        : `Not recording ${currentOrigin}`;
      toggle.hidden = false;
      toggle.textContent = isAllowed ? 'Stop recording this site' : 'Record this site';
      toggle.onclick = () => {
        const next = isAllowed
          ? allowed.filter(origin => origin !== currentOrigin)
          : allowed.concat([currentOrigin]);
        writeAllowed(next, () => render(currentOrigin));
      };
    }

    const list = document.getElementById('sites');
    list.textContent = '';
    if (allowed.length === 0) {
      const empty = document.createElement('li');
      empty.className = 'muted';
      empty.textContent = 'None. Nothing is being recorded.';
      list.appendChild(empty);
    }
    allowed.forEach((origin) => {
      const item = document.createElement('li');
      const label = document.createElement('code');
      label.textContent = origin;
      const remove = document.createElement('button');
      remove.textContent = 'Remove';
      remove.onclick = () => {
        writeAllowed(allowed.filter(entry => entry !== origin), () => render(currentOrigin));
      };
      item.appendChild(label);
      item.appendChild(remove);
      list.appendChild(item);
    });
  });
}

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  let origin = null;
  try {
    origin = new URL(tabs[0].url).origin;
  } catch (_) {
    origin = null;
  }
  render(origin);
});
