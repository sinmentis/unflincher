// site/assets/js/demo.js -- pure, framework-free logic for the Unflincher public static
// demo. It makes no network request except loading the committed synthetic fixture, and it
// stores nothing in the browser: no cookies and no web storage. Functions are exported for
// Node testing (tests/test_site_demo_js.py) following the same CommonJS harness pattern used by
// src/unflincher/static/js/*.js.

var DEMO_VIEWS = ["timeline", "entry", "report", "conversation", "workshop"];
var SELF_HOSTED_NOTICE = "Available in the self-hosted app.";
var GITHUB_URL = "https://github.com/sinmentis/unflincher";

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function normalizeView(raw) {
  var key = String(raw == null ? "" : raw).trim().toLowerCase();
  return DEMO_VIEWS.indexOf(key) === -1 ? "timeline" : key;
}

function viewFromQuery(search) {
  try {
    return new URLSearchParams(search || "").get("view");
  } catch (error) {
    return null;
  }
}

function parseFixture(text) {
  var data;
  try {
    data = JSON.parse(text);
  } catch (error) {
    return {ok: false, error: "invalid-json"};
  }
  if (!data || typeof data !== "object" || !data.meta || data.meta.synthetic !== true) {
    return {ok: false, error: "not-synthetic"};
  }
  return {ok: true, data: data};
}

function findEntry(data, entryId) {
  var entries = (data && data.entries) || [];
  for (var i = 0; i < entries.length; i++) {
    if (entries[i].id === entryId) return entries[i];
  }
  return entries[0] || null;
}

function renderTimeline(data) {
  var entries = (data && data.entries) || [];
  var items = entries
    .map(function (entry) {
      return (
        '<li><button type="button" class="demo-entry-link" data-entry-id="' +
        escapeHtml(entry.id) +
        '"><span class="demo-entry-date">' +
        escapeHtml(entry.date) +
        '</span><span class="demo-entry-title">' +
        escapeHtml(entry.title) +
        "</span></button></li>"
      );
    })
    .join("");
  return "<h3>Timeline</h3><ol class=\"demo-list\">" + items + "</ol>";
}

function renderEntry(data, entryId) {
  var entry = findEntry(data, entryId);
  if (!entry) return "<h3>Entry and commentary</h3><p>No entry available.</p>";
  return (
    "<h3>Entry and commentary</h3>" +
    '<article class="demo-entry">' +
    '<p class="demo-entry-meta">' +
    escapeHtml(entry.date) +
    "</p><h4>" +
    escapeHtml(entry.title) +
    '</h4><p class="demo-entry-body">' +
    escapeHtml(entry.body) +
    '</p><div class="demo-commentary"><h5>Commentary</h5><p>' +
    escapeHtml(entry.commentary) +
    '</p></div><button type="button" class="demo-action" disabled>Regenerate commentary</button>' +
    '<p class="demo-locked">' +
    escapeHtml(SELF_HOSTED_NOTICE) +
    "</p></article>"
  );
}

function renderReport(data) {
  var report = (data && data.report) || {sections: []};
  var byId = {};
  ((data && data.entries) || []).forEach(function (entry) {
    byId[entry.id] = entry;
  });
  var sections = (report.sections || [])
    .map(function (section) {
      var evidence = (section.evidence || [])
        .map(function (ref) {
          var entry = byId[ref];
          if (!entry) return "";
          return (
            '<li><span class="demo-entry-date">' +
            escapeHtml(entry.date) +
            "</span> " +
            escapeHtml(entry.title) +
            "</li>"
          );
        })
        .join("");
      return (
        '<section class="demo-report-section"><h4>' +
        escapeHtml(section.heading) +
        "</h4><p>" +
        escapeHtml(section.body) +
        "</p>" +
        (evidence ? '<ul class="demo-evidence">' + evidence + "</ul>" : "") +
        "</section>"
      );
    })
    .join("");
  return "<h3>" + escapeHtml(report.title || "Life Report") + "</h3>" + sections;
}

function renderConversation(data) {
  var conversation = (data && data.conversation) || {messages: []};
  var messages = (conversation.messages || [])
    .map(function (message) {
      return (
        '<li class="demo-message demo-message--' +
        escapeHtml(message.role) +
        '"><span class="demo-role">' +
        escapeHtml(message.role) +
        "</span><p>" +
        escapeHtml(message.text) +
        "</p></li>"
      );
    })
    .join("");
  return (
    "<h3>" +
    escapeHtml(conversation.title || "Conversation") +
    '</h3><ul class="demo-conversation">' +
    messages +
    '</ul><button type="button" class="demo-action" disabled>Continue conversation</button>' +
    '<p class="demo-locked">' +
    escapeHtml(SELF_HOSTED_NOTICE) +
    "</p>"
  );
}

function renderWorkshop(data) {
  var workshop = (data && data.workshop) || {personas: []};
  var personas = (workshop.personas || [])
    .map(function (persona) {
      return (
        '<section class="demo-persona"><h4>' +
        escapeHtml(persona.name) +
        '</h4><p class="demo-persona-prompt"><span>Prompt</span> ' +
        escapeHtml(persona.prompt) +
        '</p><p class="demo-persona-sample"><span>Sample</span> ' +
        escapeHtml(persona.sample) +
        "</p></section>"
      );
    })
    .join("");
  return (
    "<h3>Prompt Workshop</h3>" +
    personas +
    '<button type="button" class="demo-action" disabled>Apply and regenerate</button>' +
    '<p class="demo-locked">' +
    escapeHtml(SELF_HOSTED_NOTICE) +
    "</p>"
  );
}

function renderView(viewKey, data, entryId) {
  switch (normalizeView(viewKey)) {
    case "entry":
      return renderEntry(data, entryId);
    case "report":
      return renderReport(data);
    case "conversation":
      return renderConversation(data);
    case "workshop":
      return renderWorkshop(data);
    default:
      return renderTimeline(data);
  }
}

function renderError() {
  return (
    '<div class="demo-error" role="alert"><p>The sample data could not be loaded.</p>' +
    '<p><a href="' +
    GITHUB_URL +
    '">View the source on GitHub</a></p></div>'
  );
}

async function initDemo(rootEl, fetchImpl, fixtureUrl) {
  var stage = rootEl.querySelector("[data-demo-stage]");
  var fallbackHtml = stage.innerHTML;
  var buttons = Array.prototype.slice.call(rootEl.querySelectorAll("[data-view]"));
  var queryView = null;
  if (typeof window !== "undefined" && window.location) {
    queryView = viewFromQuery(window.location.search);
  }
  var state = {
    view: normalizeView(queryView != null ? queryView : rootEl.getAttribute("data-initial-view")),
    data: null,
    entryId: null,
  };

  function paint() {
    buttons.forEach(function (button) {
      var active = normalizeView(button.getAttribute("data-view")) === state.view;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-current", active ? "true" : "false");
    });
    stage.innerHTML = renderView(state.view, state.data, state.entryId);
    Array.prototype.slice.call(stage.querySelectorAll("[data-entry-id]")).forEach(function (link) {
      link.addEventListener("click", function () {
        state.entryId = link.getAttribute("data-entry-id");
        state.view = "entry";
        paint();
      });
    });
  }

  try {
    var response = await fetchImpl(fixtureUrl, {cache: "no-store"});
    if (!response.ok) throw new Error("fixture status " + response.status);
    var parsed = parseFixture(await response.text());
    if (!parsed.ok) throw new Error(parsed.error);
    state.data = parsed.data;
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        state.view = normalizeView(button.getAttribute("data-view"));
        paint();
      });
    });
    paint();
  } catch (error) {
    stage.innerHTML = renderError() + fallbackHtml;
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.slice.call(document.querySelectorAll("[data-demo-root]")).forEach(function (root) {
      initDemo(root, window.fetch.bind(window), root.getAttribute("data-fixture"));
    });
  });
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    DEMO_VIEWS: DEMO_VIEWS,
    escapeHtml: escapeHtml,
    normalizeView: normalizeView,
    viewFromQuery: viewFromQuery,
    parseFixture: parseFixture,
    findEntry: findEntry,
    renderView: renderView,
    renderError: renderError,
    initDemo: initDemo,
  };
}
