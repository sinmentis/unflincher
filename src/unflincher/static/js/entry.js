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

  // Segmented control (Body / Reflection / Conversation): a real ARIA tablist with roving
  // tabindex. Each tab shows exactly one panel -- replaces the former single long-scroll layout
  // and its IntersectionObserver-driven jump links, so switching sections is instant instead of
  // a scroll position guess.
  const tabs = Array.from(doc.querySelectorAll(".entry-segmented-tab"));
  const thumb = doc.querySelector(".entry-segmented-thumb");
  if (tabs.length && thumb) {
    const panelFor = (tab) => doc.getElementById(tab.getAttribute("aria-controls"));

    const positionThumb = (tab, {instant = false} = {}) => {
      if (instant) thumb.style.transitionDuration = "0ms";
      thumb.style.width = `${tab.offsetWidth}px`;
      thumb.style.transform = `translateX(${tab.offsetLeft}px)`;
      if (instant) {
        const restoreTransition = () => {
          thumb.style.transitionDuration = "";
        };
        if (typeof window !== "undefined" && "requestAnimationFrame" in window) {
          window.requestAnimationFrame(restoreTransition);
        } else {
          restoreTransition();
        }
      }
    };

    const activate = (tab, {focus = false, instant = false} = {}) => {
      tabs.forEach((other) => {
        const isActive = other === tab;
        other.setAttribute("aria-selected", String(isActive));
        other.tabIndex = isActive ? 0 : -1;
        const panel = panelFor(other);
        if (panel) panel.hidden = !isActive;
        panel?.classList.toggle("is-active", isActive);
      });
      positionThumb(tab, {instant});
      if (focus) tab.focus();
    };

    tabs.forEach((tab, index) => {
      tab.tabIndex = tab.getAttribute("aria-selected") === "true" ? 0 : -1;
      tab.addEventListener("click", () => activate(tab));
      tab.addEventListener("keydown", (event) => {
        let target = null;
        if (event.key === "ArrowRight") target = tabs[(index + 1) % tabs.length];
        else if (event.key === "ArrowLeft") target = tabs[(index - 1 + tabs.length) % tabs.length];
        else if (event.key === "Home") target = tabs[0];
        else if (event.key === "End") target = tabs[tabs.length - 1];
        if (!target) return;
        event.preventDefault();
        activate(target, {focus: true, instant: true});
      });
    });

    if (typeof window !== "undefined" && "requestAnimationFrame" in window) {
      const repositionActiveThumb = () => {
        const selected = tabs.find((tab) => tab.getAttribute("aria-selected") === "true") || tabs[0];
        positionThumb(selected);
      };
      window.requestAnimationFrame(repositionActiveThumb);
      // Each tab's width/offset is layout-dependent (equal thirds of the row), so a window
      // resize (or rotation) without a click must resnap the thumb -- otherwise it's left at a
      // stale pixel position from before the resize.
      window.addEventListener("resize", () => window.requestAnimationFrame(repositionActiveThumb));
    }
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => initEntryPage(document));
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = {initEntryPage};
}
