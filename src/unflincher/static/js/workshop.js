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
    throw error;
  }
  const {job_id: jobId} = await regenResponse.json();
  return jobId;
}

function initWorkshopPage(doc = document) {
  const notice = doc.getElementById("workshop-notice");
  const modelNotice = doc.getElementById("model-notice");
  const payload = () => ({
    draft_prompt: doc.getElementById("prompt-draft").value,
    model: doc.getElementById("model-select").value,
  });

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
    await streamInto("/workshop/test-run", {
      ...payload(),
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
        body: JSON.stringify(payload()),
      });
      if (!response.ok) throw new Error(`apply failed: ${response.status}`);
      setNotice(notice, applyButton.dataset.savedLabel, "saved");
    } catch {
      setNotice(notice, window.UI_MESSAGES.requestFailed, "failed");
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
      const jobId = await applyAndRegenerate(fetch, payload(), getCsrfToken());
      const progress = doc.createElement("div");
      progress.id = "regen-progress";
      progress.setAttribute("hx-get", `/workshop/jobs/${jobId}/progress`);
      progress.setAttribute("hx-trigger", "load, every 2s");
      progress.setAttribute("hx-swap", "outerHTML");
      holder.replaceChildren(progress);
      htmx.process(progress);
      confirmation.hidden = true;
    } catch (error) {
      holder.replaceChildren();
      const message = error.status === 409 ? window.UI_MESSAGES.busy : window.UI_MESSAGES.requestFailed;
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
  module.exports = {applyAndRegenerate, initWorkshopPage};
}
