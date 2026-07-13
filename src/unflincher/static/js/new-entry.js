function localDateString(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function initNewEntryPage(doc = document, storage = window.localStorage) {
  const form = doc.getElementById("new-entry-form");
  if (!form) return;
  const dateInput = doc.getElementById("new-date");
  const titleInput = doc.getElementById("new-title");
  const contentInput = doc.getElementById("new-content");
  const draftStatus = doc.getElementById("draft-status");
  const dateError = doc.getElementById("new-date-error");
  const notice = doc.getElementById("new-entry-notice");
  const submit = doc.getElementById("save-entry");
  const today = localDateString(new Date());
  dateInput.value = today;
  dateInput.max = today;

  const draft = loadDraft(storage);
  if (draft) {
    if (draft.date) dateInput.value = draft.date;
    titleInput.value = draft.title || "";
    contentInput.value = draft.content || "";
    draftStatus.textContent = form.dataset.draftSaved;
  }

  let saveTimer = null;
  const scheduleSave = () => {
    draftStatus.textContent = form.dataset.draftSaving;
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => {
      saveDraft(storage, {
        date: dateInput.value,
        title: titleInput.value,
        content: contentInput.value,
      });
      draftStatus.textContent = form.dataset.draftSaved;
    }, 500);
  };
  [dateInput, titleInput, contentInput].forEach((field) => field.addEventListener("input", scheduleSave));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (form.dataset.busy === "1") return;
    clearNotice(notice);
    dateError.hidden = true;
    form.dataset.busy = "1";
    submit.disabled = true;
    try {
      const response = await fetch("/new", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": getCsrfToken()},
        body: JSON.stringify({
          title: titleInput.value,
          content: contentInput.value,
          entry_date: dateInput.value,
        }),
      });
      if (!response.ok) {
        const message = response.status === 400 ? form.dataset.saveFailed : form.dataset.requestFailed;
        if (response.status === 400) {
          dateError.textContent = message;
          dateError.hidden = false;
          dateInput.focus();
        }
        setNotice(notice, message, "failed");
        return;
      }
      const {entry_id: entryId} = await response.json();
      window.clearTimeout(saveTimer);
      clearDraft(storage);
      window.location.href = `/entry/${entryId}`;
    } catch {
      setNotice(notice, form.dataset.requestFailed, "failed");
    } finally {
      delete form.dataset.busy;
      submit.disabled = false;
    }
  });
}
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => initNewEntryPage(document));
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = {localDateString, initNewEntryPage};
}
