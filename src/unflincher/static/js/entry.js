// src/unflincher/static/js/entry.js -- entry-detail interactions (Task 4). Loaded only by
// entry_detail.html via {% block scripts %}, after the shared app.js globals are defined.
function initEntryPage(doc = document) {
  const notice = doc.getElementById("commentary-notice");
  const trigger = doc.getElementById("run-commentary") || doc.getElementById("retry-commentary");
  if (trigger) {
    trigger.addEventListener("click", async () => {
      trigger.disabled = true;
      clearNotice(notice);
      try {
        const response = await fetch(trigger.dataset.endpoint, {
          method: "POST",
          headers: {"X-CSRF-Token": getCsrfToken()},
        });
        if (response.status === 409) {
          setNotice(notice, window.UI_MESSAGES.busy, "busy");
          return;
        }
        if (!response.ok) throw new Error(`commentary failed: ${response.status}`);
        window.location.reload();
      } catch {
        setNotice(notice, window.UI_MESSAGES.requestFailed, "failed");
      } finally {
        trigger.disabled = false;
      }
    });
  }

  const form = doc.getElementById("entry-chat-composer");
  if (form) {
    bindComposer(form, async (message) => {
      const input = doc.getElementById("chat-input");
      const target = doc.getElementById("chat-stream");
      await streamInto(form.dataset.endpoint, {message}, target, () => {
        input.value = "";
        window.location.reload();
      });
    });
  }

  const toc = doc.querySelector(".entry-margin-index");
  if (toc && "IntersectionObserver" in window) {
    const sections = ["diary-text", "ai-commentary", "chat-section"]
      .map((id) => doc.getElementById(id))
      .filter(Boolean);
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        toc.querySelectorAll("[data-jump]").forEach((link) => {
          link.classList.toggle("is-active", link.hash === `#${entry.target.id}`);
        });
      }
    }, {rootMargin: "-35% 0px -55% 0px"});
    sections.forEach((section) => observer.observe(section));
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => initEntryPage(document));
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = {initEntryPage};
}
