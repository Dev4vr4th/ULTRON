const sitePrefixEl = document.getElementById("sitePrefix");
const bridgeTokenEl = document.getElementById("bridgeToken");
const enabledEl = document.getElementById("enabled");
const statusEl = document.getElementById("status");
const saveBtn = document.getElementById("save");

chrome.storage.local.get(["sitePrefix", "bridgeToken", "enabled"], (data) => {
  sitePrefixEl.value = data.sitePrefix || "";
  bridgeTokenEl.value = data.bridgeToken || "";
  enabledEl.checked = !!data.enabled;
});

saveBtn.addEventListener("click", () => {
  const sitePrefix = sitePrefixEl.value.trim();
  const bridgeToken = bridgeTokenEl.value.trim();
  const enabled = enabledEl.checked && !!sitePrefix && !!bridgeToken;

  chrome.storage.local.set({ sitePrefix, bridgeToken, enabled }, () => {
    if (enabledEl.checked && !enabled) {
      statusEl.textContent = "Need both a site prefix and a bridge token before this can watch.";
    } else {
      statusEl.textContent = enabled
        ? "Saved — watching " + sitePrefix
        : "Saved — currently off.";
    }
  });
});
