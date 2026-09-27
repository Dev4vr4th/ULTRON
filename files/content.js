// content.js — runs on every page, but does nothing unless the user has
// explicitly enabled watching AND set a site prefix that this page's URL
// matches. Ships inert by default; configure it from the toolbar popup.

(function () {
  let enabled = false;
  let sitePrefix = "";

  function siteMatches() {
    return enabled && sitePrefix && location.href.startsWith(sitePrefix);
  }

  function send(event) {
    if (!siteMatches()) return;
    try {
      chrome.runtime.sendMessage({
        kind: "ultron-finding",
        event: { ...event, url: location.href, timestamp: Date.now() },
      });
    } catch (e) {
      // Extension context can go away on reload; nothing to do about it here.
    }
  }

  chrome.storage.local.get(["enabled", "sitePrefix"], (data) => {
    enabled = !!data.enabled;
    sitePrefix = data.sitePrefix || "";
  });
  chrome.storage.onChanged.addListener((changes) => {
    if (changes.enabled) enabled = !!changes.enabled.newValue;
    if (changes.sitePrefix) sitePrefix = changes.sitePrefix.newValue || "";
  });

  // Wrap console.error / console.warn without hiding them from devtools.
  const origError = console.error.bind(console);
  const origWarn = console.warn.bind(console);
  console.error = (...args) => {
    send({ type: "console.error", message: args.map(String).join(" ") });
    origError(...args);
  };
  console.warn = (...args) => {
    send({ type: "console.warn", message: args.map(String).join(" ") });
    origWarn(...args);
  };

  window.addEventListener("error", (e) => {
    send({ type: "uncaught-exception", message: `${e.message} (${e.filename}:${e.lineno})` });
  });
  window.addEventListener("unhandledrejection", (e) => {
    send({ type: "unhandled-rejection", message: String(e.reason) });
  });

  // ---- DOM scan: text hidden in ways that look accidental, and controls
  // that look interactive but have nothing a screen reader/click could
  // act on. This is the one thing screenshots structurally can't catch —
  // hidden text is invisible in a screenshot by definition.
  const reportedNodes = new WeakSet();
  const INTERACTIVE_SELECTOR = 'button, a[href], input, select, textarea, [role="button"], [onclick]';

  function isEffectivelyHidden(style, rect) {
    if (style.display === "none" || style.visibility === "hidden") return "display/visibility hidden";
    if (parseFloat(style.opacity) <= 0.02) return "opacity ~0";
    if ((rect.width <= 1 && rect.height <= 1) && style.overflow === "hidden") return "collapsed to 0px with overflow hidden";
    if (parseFloat(style.textIndent) <= -500) return "text-indent pushed far off-screen";
    if (rect.right < -100 || rect.left > window.innerWidth + 1000 || rect.bottom < -1000) return "positioned far off-screen";
    return null;
  }

  function looksSameAsBackground(style) {
    // Cheap contrast check: identical color/background-color is almost
    // always a mistake, not a design choice.
    return style.color && style.backgroundColor &&
      style.color.replace(/\s/g, "") === style.backgroundColor.replace(/\s/g, "") &&
      style.color !== "rgba(0, 0, 0, 0)";
  }

  function scanForHiddenText(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, {
      acceptNode: (el) => (el.textContent || "").trim().length > 2
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP,
    });
    let node;
    let checked = 0;
    while ((node = walker.nextNode()) && checked < 4000) {
      checked++;
      if (reportedNodes.has(node)) continue;
      if (node.getAttribute && node.getAttribute("aria-hidden") === "true") continue; // deliberate
      // Only flag leaf-ish elements carrying their own direct text, to
      // avoid reporting the same hidden text once per ancestor.
      const hasDirectText = Array.from(node.childNodes)
        .some((n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim().length > 2);
      if (!hasDirectText) continue;

      const style = getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      const hiddenReason = isEffectivelyHidden(style, rect);
      const sameColor = looksSameAsBackground(style);
      if (hiddenReason || sameColor) {
        reportedNodes.add(node);
        const snippet = node.textContent.trim().slice(0, 80);
        send({
          type: "hidden-text",
          message: `Text possibly unintentionally hidden (${hiddenReason || "same color as its background"}): "${snippet}"`,
        });
      }
    }
  }

  function scanForDeadControls(root) {
    root.querySelectorAll(INTERACTIVE_SELECTOR).forEach((el) => {
      if (reportedNodes.has(el)) return;
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return; // not actually rendered, not our concern
      const hasHandler = !!el.onclick || el.hasAttribute("onclick");
      const isNativeInteractive = /^(a|button|input|select|textarea)$/i.test(el.tagName);
      const accessibleName = (el.getAttribute("aria-label") || el.textContent || el.getAttribute("title") || "").trim();

      if (style.pointerEvents === "none" && (hasHandler || isNativeInteractive)) {
        reportedNodes.add(el);
        send({ type: "dead-control", message: `"${accessibleName || el.tagName}" looks interactive but has pointer-events:none — it can't be clicked.` });
        return;
      }
      if (!isNativeInteractive && !hasHandler && el.getAttribute("role") === "button") {
        reportedNodes.add(el);
        send({ type: "dead-control", message: `"${accessibleName || "unnamed element"}" has role="button" but no click handler found.` });
        return;
      }
      if (!accessibleName && isNativeInteractive) {
        reportedNodes.add(el);
        send({ type: "accessibility", message: `${el.tagName.toLowerCase()} has no accessible name (no text, aria-label, or title) — screen readers can't describe it.` });
      }
    });
  }

  function runScan() {
    if (!siteMatches()) return;
    try {
      scanForHiddenText(document.body);
      scanForDeadControls(document.body);
    } catch (e) {
      // Best-effort — a scan error on one page shouldn't break anything else.
    }
  }

  // Scan once things have rendered, then again whenever the page changes
  // meaningfully (debounced), so SPA route changes get picked up too.
  window.addEventListener("load", () => setTimeout(runScan, 1500));
  let scanTimer = null;
  new MutationObserver(() => {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(runScan, 2000);
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
