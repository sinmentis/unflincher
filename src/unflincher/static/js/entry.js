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
        if (!response.ok) {
          // Surface the estimated size + model limit + actions for a stable 413
          // context_too_large exactly like streamInto does elsewhere; any other stable reason
          // (or an unparseable body) keeps the prior generic failure notice unchanged.
          const detail = await parseStableErrorDetail(response);
          setNotice(
            notice,
            stableErrorNoticeMessage(detail, window.UI_MESSAGES.requestFailed),
            "failed",
          );
          return;
        }
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

  // Two containers share the same jump links: the desktop margin index and the mobile sticky
  // tab strip (only one is ever visible at a given width -- see pages.css). Both stay in sync
  // off the same observer so whichever one is showing already reflects the active section.
  const tocs = Array.from(doc.querySelectorAll(".entry-margin-index, .entry-mobile-tabs"));
  if (tocs.length && "IntersectionObserver" in window) {
    const sections = ["diary-text", "ai-commentary", "chat-section"]
      .map((id) => doc.getElementById(id))
      .filter(Boolean);
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        tocs.forEach((toc) => {
          toc.querySelectorAll("[data-jump]").forEach((link) => {
            link.classList.toggle("is-active", link.hash === `#${entry.target.id}`);
          });
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
