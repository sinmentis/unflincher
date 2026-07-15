async function applyAndRegenerate(fetchImpl, payload, csrfToken) {
  const headers = {"Content-Type": "application/json", "X-CSRF-Token": csrfToken};
  const regenResponse = await fetchImpl("/workshop/apply-all", {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  if (!regenResponse.ok) {
    const error = new Error(`apply-all failed: ${regenResponse.status}`);
    error.status = regenResponse.status;
    // Carry the stable generation-safety detail (estimated size/limit/reason) along with the
    // error so the caller can render the same localized capacity notice streamInto() shows
    // elsewhere, instead of only ever a generic failure message.
    error.detail = await parseStableErrorDetail(regenResponse);
    throw error;
  }
  // presetKey is the SERVER's resolved classification (read back from the actual persisted row --
  // see routes/workshop.py), never the browser's sent intent: a stale/forged/edited intent (or
  // Custom text that happens to exactly match a shipped preset) must not misdrive the caller's
  // "active Perspective" label update.
  const {job_id: jobId, preset_key: presetKey} = await regenResponse.json();
  return {jobId, presetKey};
}

// Reads the Choose-a-Perspective stage's server-rendered data blob (Task: Workshop): exact
// preset text/name per key, keyed by preset key plus "custom". Never reconstructs prompt text
// itself -- this is read-only data authored by perspectives.py (see routes/workshop.py).
function readPerspectiveData(doc = document) {
  const node = doc.getElementById("perspective-data");
  if (!node) return {};
  try {
    return JSON.parse(node.textContent || "{}");
  } catch {
    return {};
  }
}

function initWorkshopPage(doc = document) {
  const notice = doc.getElementById("workshop-notice");
  const modelNotice = doc.getElementById("model-notice");
  const textarea = doc.getElementById("prompt-draft");
  const perspectiveData = readPerspectiveData(doc);
  const perspectiveRadios = Array.from(doc.querySelectorAll('input[name="perspective-choice"]'));
  const activePerspectiveLabel = doc.querySelector('[data-role="active-perspective"]');

  // The draft's CURRENT preset intent, one of the shipped keys or null (Custom). Starts at
  // whichever radio the server pre-checked (matching the persisted active preset_key); the
  // server always re-derives the REAL stored value from exact text, so this is only an
  // optimistic client-side intent hint (see ApplyRequest's docstring in routes/workshop.py).
  let currentPresetKey = null;
  const initiallyChecked = perspectiveRadios.find((radio) => radio.checked);
  if (initiallyChecked && initiallyChecked.value !== "custom") {
    currentPresetKey = initiallyChecked.value;
  }

  function selectRadioForKey(key) {
    const value = key || "custom";
    const radio = perspectiveRadios.find((candidate) => candidate.value === value);
    if (radio) radio.checked = true;
  }

  perspectiveRadios.forEach((radio) => {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      if (radio.value === "custom") {
        // Selecting Custom never overwrites the textarea -- it just stops claiming a preset.
        currentPresetKey = null;
        return;
      }
      const preset = perspectiveData[radio.value];
      if (preset && typeof preset.prompt === "string") textarea.value = preset.prompt;
      currentPresetKey = radio.value;
    });
  });

  // Any edit that makes the textarea differ from the currently selected preset's exact text
  // switches the selection to Custom immediately (plan requirement 3) -- but never the reverse:
  // typing back to an exact match is left for the server's own exact-text classification on
  // Apply/Apply-all, not re-detected here.
  textarea.addEventListener("input", () => {
    if (!currentPresetKey) return;
    const preset = perspectiveData[currentPresetKey];
    if (preset && textarea.value !== preset.prompt) {
      currentPresetKey = null;
      selectRadioForKey(null);
    }
  });

  function updateActivePerspectiveLabel(presetKey) {
    if (!activePerspectiveLabel) return;
    const entry = perspectiveData[presetKey || "custom"];
    const name = entry ? entry.name : "";
    const template = (window.UI_MESSAGES && window.UI_MESSAGES.activePerspectiveLabel) || "";
    activePerspectiveLabel.textContent = template.replace("{name}", name);
  }

  const basePayload = () => ({
    draft_prompt: textarea.value,
    model: doc.getElementById("model-select").value,
  });
  const applyPayload = () => ({...basePayload(), preset_key: currentPresetKey});

  const refreshModels = doc.getElementById("refresh-models");
  refreshModels.addEventListener("click", async () => {
    refreshModels.disabled = true;
    setNotice(modelNotice, window.UI_MESSAGES.working, "busy");
    try {
      const response = await fetch("/workshop/refresh-models", {
        method: "POST",
        headers: {"X-CSRF-Token": getCsrfToken()},
      });
      if (!response.ok) {
        const error = new Error(`refresh failed: ${response.status}`);
        error.status = response.status;
        throw error;
      }
      window.location.reload();
    } catch (error) {
      const message = error.status === 409 ? window.UI_MESSAGES.busy : window.UI_MESSAGES.requestFailed;
      setNotice(modelNotice, message, error.status === 409 ? "busy" : "failed");
      refreshModels.disabled = false;
    }
  });

  const runTest = doc.getElementById("run-test");
  runTest.addEventListener("click", async () => {
    if (runTest.disabled) return;
    runTest.disabled = true;
    // test-run NEVER accepts or persists a preset_key (see TestRunRequest) -- basePayload() only.
    await streamInto("/workshop/test-run", {
      ...basePayload(),
      entry_id: Number.parseInt(doc.getElementById("test-entry").value, 10),
    }, doc.getElementById("preview-stream"));
    runTest.disabled = false;
  });

  const applyButton = doc.getElementById("apply-btn");
  applyButton.addEventListener("click", async () => {
    applyButton.disabled = true;
    clearNotice(notice);
    try {
      const response = await fetch("/workshop/apply", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": getCsrfToken()},
        body: JSON.stringify(applyPayload()),
      });
      if (!response.ok) {
        const error = new Error(`apply failed: ${response.status}`);
        error.status = response.status;
        error.detail = await parseStableErrorDetail(response);
        throw error;
      }
      const {preset_key: resolvedPresetKey} = await response.json();
      updateActivePerspectiveLabel(resolvedPresetKey);
      setNotice(notice, applyButton.dataset.savedLabel, "saved");
    } catch (error) {
      const message = error.status === 409
        ? window.UI_MESSAGES.busy
        : stableErrorNoticeMessage(error.detail, window.UI_MESSAGES.requestFailed);
      setNotice(notice, message, error.status === 409 ? "busy" : "failed");
    } finally {
      applyButton.disabled = false;
    }
  });

  const confirmation = doc.getElementById("apply-all-confirmation");
  const applyAllButton = doc.getElementById("apply-all-btn");
  const confirmButton = confirmation.querySelector("[data-confirm]");
  const cancelButton = confirmation.querySelector("[data-cancel]");
  applyAllButton.addEventListener("click", () => {
    confirmation.hidden = false;
    confirmButton.focus();
  });
  cancelButton.addEventListener("click", () => {
    confirmation.hidden = true;
    applyAllButton.focus();
  });
  confirmButton.addEventListener("click", async () => {
    const holder = doc.getElementById("regen-progress-holder");
    clearNotice(notice);
    confirmButton.disabled = true;
    cancelButton.disabled = true;
    applyAllButton.disabled = true;
    holder.replaceChildren(doc.getElementById("loading-state-template").content.cloneNode(true));
    try {
      const {jobId, presetKey} = await applyAndRegenerate(fetch, applyPayload(), getCsrfToken());
      const progress = doc.createElement("div");
      progress.id = "regen-progress";
      progress.setAttribute("hx-get", `/workshop/jobs/${jobId}/progress`);
      progress.setAttribute("hx-trigger", "load, every 2s");
      progress.setAttribute("hx-swap", "outerHTML");
      holder.replaceChildren(progress);
      htmx.process(progress);
      // Apply-all activates the prompt+preset atomically before this response returns (see
      // routes/workshop.py) -- update the label from the SERVER's resolved classification, never
      // the browser's sent intent (currentPresetKey): a stale/forged/edited intent, or Custom
      // text that happens to exactly match a shipped preset, must not misdrive this label.
      updateActivePerspectiveLabel(presetKey);
      confirmation.hidden = true;
    } catch (error) {
      holder.replaceChildren();
      const message = error.status === 409
        ? window.UI_MESSAGES.busy
        : stableErrorNoticeMessage(error.detail, window.UI_MESSAGES.requestFailed);
      setNotice(notice, message, error.status === 409 ? "busy" : "failed");
    } finally {
      confirmButton.disabled = false;
      cancelButton.disabled = false;
      applyAllButton.disabled = false;
    }
  });

  const language = doc.getElementById("lang-select");
  language.addEventListener("change", async () => {
    language.disabled = true;
    try {
      const response = await fetch("/workshop/language", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": getCsrfToken()},
        body: JSON.stringify({lang: language.value}),
      });
      if (!response.ok) throw new Error(`language failed: ${response.status}`);
      window.location.reload();
    } catch {
      setNotice(notice, window.UI_MESSAGES.requestFailed, "failed");
      language.disabled = false;
    }
  });
}
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => initWorkshopPage(document));
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = {applyAndRegenerate, initWorkshopPage, readPerspectiveData};
}
