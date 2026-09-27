// background.js — buffers findings and POSTs them in small batches to
// gesture_bridge.py running on this machine. Never talks to any other
// destination; the bridge URL below is hardcoded, not user-configurable,
// so a compromised popup couldn't be used to exfiltrate this data
// elsewhere.

const BRIDGE_URLS = ["http://127.0.0.1:8765/console-log", "http://localhost:8765/console-log"];
const FLUSH_INTERVAL_MS = 5000;
const MAX_BUFFER = 200;

let buffer = [];

function getConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.get(["enabled", "sitePrefix", "bridgeToken"], (data) => resolve(data));
  });
}

async function flush() {
  if (buffer.length === 0) return;
  const { enabled, bridgeToken } = await getConfig();
  if (!enabled || !bridgeToken) {
    buffer = []; // don't accumulate forever while disabled/unconfigured
    return;
  }
  const events = buffer.splice(0, MAX_BUFFER);
  for (const url of BRIDGE_URLS) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Ultron-Token": bridgeToken },
        body: JSON.stringify({ events }),
      });
      if (res.ok) return; // sent successfully, don't also try the other host
    } catch (e) {
      // bridge likely not running — try the next URL / give up silently
    }
  }
}

setInterval(flush, FLUSH_INTERVAL_MS);

chrome.runtime.onMessage.addListener(async (msg) => {
  if (msg && msg.kind === "ultron-finding") {
    const { enabled, sitePrefix } = await getConfig();
    if (!enabled || !sitePrefix || !msg.event?.url?.startsWith(sitePrefix)) return;
    if (buffer.length < MAX_BUFFER) buffer.push(msg.event);
  }
});

// Failed network requests (4xx/5xx and connection-level failures) for the
// configured site only. Filtered inside the callback (not via a static
// urls filter) so it can react to the user changing sitePrefix without
// needing to re-register the listener.
chrome.webRequest.onCompleted.addListener(
  async (details) => {
    if (details.statusCode < 400) return;
    const { enabled, sitePrefix } = await getConfig();
    if (!enabled || !sitePrefix || !details.url.startsWith(sitePrefix)) return;
    if (buffer.length < MAX_BUFFER) {
      buffer.push({
        type: "network-error",
        status: details.statusCode,
        message: `${details.method} request failed`,
        url: details.url,
        timestamp: Date.now(),
      });
    }
  },
  { urls: ["<all_urls>"] }
);

chrome.webRequest.onErrorOccurred.addListener(
  async (details) => {
    const { enabled, sitePrefix } = await getConfig();
    if (!enabled || !sitePrefix || !details.url.startsWith(sitePrefix)) return;
    if (buffer.length < MAX_BUFFER) {
      buffer.push({
        type: "network-error",
        message: `${details.method} request error: ${details.error}`,
        url: details.url,
        timestamp: Date.now(),
      });
    }
  },
  { urls: ["<all_urls>"] }
);
