// site/assets/js/site.js -- progressive reveal for the landing page. Exports pure functions for
// Node testing (tests/test_site_landing.py) following the same CommonJS harness pattern as the
// rest of the site. It stores nothing and makes no network request.

function prefersReducedMotion(win) {
  return !!(win && win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

function revealOnScroll(doc, win) {
  var targets = Array.prototype.slice.call(doc.querySelectorAll("[data-reveal]"));
  function revealAll() {
    targets.forEach(function (el) {
      el.classList.add("is-revealed");
    });
  }
  if (prefersReducedMotion(win) || !("IntersectionObserver" in win)) {
    revealAll();
    return;
  }
  var observer = new win.IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-revealed");
          observer.unobserve(entry.target);
        }
      });
    },
    {rootMargin: "0px 0px -10% 0px"}
  );
  targets.forEach(function (el) {
    observer.observe(el);
  });
}

if (typeof document !== "undefined") {
  document.documentElement.classList.add("js-reveal");
  document.addEventListener("DOMContentLoaded", function () {
    revealOnScroll(document, window);
  });
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = {prefersReducedMotion: prefersReducedMotion, revealOnScroll: revealOnScroll};
}
