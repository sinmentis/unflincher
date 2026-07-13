// src/unflincher/static/js/chat.js
function isValidSessionTitle(value) {
  return value.trim().length > 0;
}

function initChatPage(doc = document) {
  const notice = doc.getElementById("chat-notice");
  doc.querySelectorAll("[data-rename-session]").forEach((button) => {
    button.addEventListener("click", () => {
      const form = doc.getElementById(`rename-session-${button.dataset.renameSession}`);
      form.hidden = false;
      form.querySelector("input").focus();
    });
  });

  doc.querySelectorAll(".session-inline-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = form.querySelector("input");
      const title = input.value.trim();
      if (!isValidSessionTitle(title)) {
        setNotice(notice, window.UI_MESSAGES.titleRequired, "failed");
        input.focus();
        return;
      }
      clearNotice(notice);
      try {
        const response = await fetch(`/chat/${form.dataset.sessionId}/rename`, {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-CSRF-Token": getCsrfToken()},
          body: JSON.stringify({title}),
        });
        if (!response.ok) throw new Error(`rename failed: ${response.status}`);
        window.location.reload();
      } catch {
        setNotice(notice, window.UI_MESSAGES.requestFailed, "failed");
        input.focus();
      }
    });
    form.querySelector("[data-cancel-rename]").addEventListener("click", () => {
      const input = form.querySelector("input");
      input.value = input.defaultValue;
      form.hidden = true;
      doc.querySelector(`[data-rename-session="${form.dataset.sessionId}"]`).focus();
    });
  });

  doc.querySelectorAll("[data-delete-session]").forEach((button) => {
    button.addEventListener("click", () => {
      const panel = doc.getElementById(`delete-session-${button.dataset.deleteSession}`);
      panel.hidden = false;
      panel.querySelector("[data-confirm]").focus();
    });
  });
  doc.querySelectorAll("[id^='delete-session-']").forEach((panel) => {
    const sessionId = panel.id.replace("delete-session-", "");
    panel.querySelector("[data-cancel]").addEventListener("click", () => {
      panel.hidden = true;
      doc.querySelector(`[data-delete-session="${sessionId}"]`).focus();
    });
    panel.querySelector("[data-confirm]").addEventListener("click", async () => {
      clearNotice(notice);
      try {
        const response = await fetch(`/chat/${sessionId}/delete`, {
          method: "POST",
          headers: {"X-CSRF-Token": getCsrfToken()},
        });
        if (!response.ok) throw new Error(`delete failed: ${response.status}`);
        window.location.href = "/chat";
      } catch {
        setNotice(notice, window.UI_MESSAGES.requestFailed, "failed");
      }
    });
  });

  const composer = doc.getElementById("general-chat-composer");
  if (composer) {
    bindComposer(composer, async (message) => {
      const input = doc.getElementById("chat-input");
      const target = doc.getElementById("chat-stream");
      await streamInto(composer.dataset.endpoint, {message}, target, (payload) => {
        input.value = "";
        if (payload.session_id) {
          window.location.href = `/chat/${payload.session_id}`;
        } else {
          window.location.reload();
        }
      });
    });
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => initChatPage(document));
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = {initChatPage, isValidSessionTitle};
}
