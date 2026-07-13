// src/unflincher/static/js/timeline.js -- client-side year filtering for the life archive
// index (Task 6). Loaded only by timeline.html via {% block scripts %}. Filtering never
// changes URLs or server queries; it only hides/shows archive rows and year dividers.
function initTimeline(doc = document) {
  const rows = [...doc.querySelectorAll("[data-year]")];
  const dividers = [...doc.querySelectorAll("[data-year-divider]")];
  const clear = doc.querySelector("[data-year-clear]");
  doc.querySelectorAll("[data-year-link]").forEach((button) => {
    button.addEventListener("click", () => {
      const year = button.dataset.yearLink;
      doc.querySelectorAll("[data-year-link]").forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      clear.disabled = false;
      clear.classList.remove("is-active");
      clear.setAttribute("aria-pressed", "false");
      rows.forEach((row) => { row.hidden = row.dataset.year !== year; });
      dividers.forEach((divider) => { divider.hidden = divider.dataset.yearDivider !== year; });
    });
  });
  clear?.addEventListener("click", () => {
    clear.disabled = true;
    clear.classList.add("is-active");
    clear.setAttribute("aria-pressed", "true");
    doc.querySelectorAll("[data-year-link]").forEach((item) => {
      item.classList.remove("is-active");
      item.setAttribute("aria-pressed", "false");
    });
    [...rows, ...dividers].forEach((item) => { item.hidden = false; });
  });
}
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => initTimeline(document));
}
if (typeof module !== "undefined" && module.exports) module.exports = {initTimeline};
