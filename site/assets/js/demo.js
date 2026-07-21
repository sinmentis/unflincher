// Static, synthetic product demo. It only fetches the committed fixture and stores nothing.

var DEMO_VIEWS = ["timeline", "entry", "report", "conversation", "write", "workshop"];
var SELF_HOSTED_NOTICE = "Self-hosted app only.";
var GITHUB_URL = "https://github.com/sinmentis/unflincher";

var APP_NAV = [
  {key: "timeline", label: "Timeline", icon: "timeline"},
  {key: "report", label: "Life Report", icon: "report"},
  {key: "conversation", label: "Conversation", icon: "chat"},
  {key: "write", label: "Write", icon: "write"},
  {key: "workshop", label: "Prompt Workshop", icon: "workshop"},
];

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

function captureFromQuery(search) {
  try {
    return new URLSearchParams(search || "").get("capture") === "1";
  } catch (error) {
    return false;
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
  return entries.length ? entries[entries.length - 1] : null;
}

function wordCount(text) {
  var normalized = String(text || "").trim();
  return normalized ? normalized.split(/\s+/).length : 0;
}

function iconSvg(name) {
  var shapes = {
    timeline: '<path d="M4 5h16M4 12h16M4 19h16"/>',
    report: '<path d="M6 3h9l3 3v15H6zM14 3v4h4M9 11h6M9 15h6"/>',
    chat: '<path d="M4 5h16v11H9l-5 4z"/>',
    write: '<path d="m4 20 4.5-1 10-10-3.5-3.5-10 10zM13.5 6.5l3.5 3.5"/>',
    workshop:
      '<path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h8M16 18h4"/>' +
      '<circle cx="16" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="14" cy="18" r="2"/>',
  };
  return (
    '<svg class="product-icon" aria-hidden="true" viewBox="0 0 24 24">' +
    (shapes[name] || "") +
    "</svg>"
  );
}

function activeAppView(view) {
  return view === "entry" ? "timeline" : view;
}

function renderAppRail(view) {
  var active = activeAppView(view);
  var links = APP_NAV.map(function (item) {
    var current = item.key === active;
    return (
      '<button type="button" class="product-nav-link' +
      (current ? " is-active" : "") +
      '" data-product-view="' +
      item.key +
      '"' +
      (current ? ' aria-current="page"' : "") +
      ">" +
      iconSvg(item.icon) +
      "<span>" +
      escapeHtml(item.label) +
      "</span></button>"
    );
  }).join("");
  return (
    '<aside class="product-rail">' +
    '<div class="product-brand">UNFLINCHER</div>' +
    '<nav class="product-nav" aria-label="Product views">' +
    links +
    "</nav></aside>"
  );
}

function renderProduct(view, content, pageClass) {
  return (
    '<div class="product-demo product-demo--' +
    escapeHtml(view) +
    '">' +
    renderAppRail(view) +
    '<main class="product-page ' +
    escapeHtml(pageClass || "") +
    '">' +
    content +
    "</main></div>"
  );
}

function renderPageHeading(title, metadata) {
  return (
    '<header class="product-heading"><h1>' +
    escapeHtml(title) +
    "</h1>" +
    (metadata ? "<p>" + escapeHtml(metadata) + "</p>" : "") +
    "</header>"
  );
}

function yearCounts(entries) {
  var counts = {};
  entries.forEach(function (entry) {
    var year = String(entry.date || "").slice(0, 4);
    if (year) counts[year] = (counts[year] || 0) + 1;
  });
  return Object.keys(counts)
    .sort()
    .reverse()
    .map(function (year) {
      return {year: year, count: counts[year]};
    });
}

function renderTimeline(data) {
  var entries = ((data && data.entries) || []).slice().reverse();
  var years = yearCounts(entries)
    .map(function (item) {
      return (
        '<button type="button" class="product-year-node" disabled>' +
        '<span class="product-year-dot">' +
        item.count +
        '</span><span class="product-year-label">' +
        item.year +
        "</span></button>"
      );
    })
    .join("");
  var rows = entries
    .map(function (entry) {
      return (
        '<button type="button" class="product-archive-row" data-entry-id="' +
        escapeHtml(entry.id) +
        '">' +
        '<time datetime="' +
        escapeHtml(entry.date) +
        '">' +
        escapeHtml(entry.date) +
        '</time><span class="product-archive-title">' +
        escapeHtml(entry.title) +
        '</span><span class="product-archive-words">' +
        wordCount(entry.body) +
        '</span><span class="product-status"><span aria-hidden="true"></span>Reflected</span></button>'
      );
    })
    .join("");
  var content =
    '<div class="product-timeline-layout"><section class="product-timeline-document">' +
    renderPageHeading("Timeline", entries.length + " entries") +
    '<section class="product-year-index"><div class="product-year-heading"><span>Filter by year</span><span>All years</span></div>' +
    '<div class="product-year-track">' +
    years +
    "</div></section>" +
    '<section class="product-archive-index"><div class="product-archive-head" aria-hidden="true"><span>Date</span><span>Entry</span><span>Words</span><span>Status</span></div>' +
    rows +
    "</section></section></div>";
  return renderProduct("timeline", content, "product-page--padded");
}

function renderParagraphs(text) {
  return String(text || "")
    .split(/\n\s*\n/)
    .filter(Boolean)
    .map(function (paragraph) {
      return "<p>" + escapeHtml(paragraph) + "</p>";
    })
    .join("");
}

function renderLockedButton(idSuffix, label, accent) {
  var noticeId = "demo-locked-" + idSuffix;
  return (
    '<div class="product-locked-action"><span class="product-lock-note" id="' +
    noticeId +
    '">' +
    escapeHtml(SELF_HOSTED_NOTICE) +
    '</span><button type="button" class="product-button' +
    (accent ? " product-button--accent" : "") +
    '" disabled aria-disabled="true" aria-describedby="' +
    noticeId +
    '">' +
    escapeHtml(label) +
    "</button></div>"
  );
}

function renderEntryPanel(entry, tab) {
  if (tab === "body") {
    return '<div class="product-reading-copy">' + renderParagraphs(entry.body) + "</div>";
  }
  if (tab === "conversation") {
    return (
      '<div class="product-entry-thread">' +
      '<article class="product-message product-message--user"><div class="product-message-stamp"><span>Me</span><time>09:24</time></div><p>What made the smaller step possible this time?</p></article>' +
      '<article class="product-message product-message--assistant"><div class="product-message-stamp"><span>Mentor</span><time>09:25</time></div><p>You stopped asking the step to prove the whole future. The entry describes one Saturday session, not a permanent identity or irreversible commitment.</p></article>' +
      '<div class="product-composer"><textarea aria-label="Conversation input" placeholder="Continue the conversation" readonly></textarea><button type="button" disabled>Send</button></div>' +
      "</div>"
    );
  }
  return (
    '<div class="product-perspective">Perspective: Analyst</div>' +
    '<div class="product-reading-copy">' +
    renderParagraphs(entry.reflection) +
    "</div>" +
    renderLockedButton("entry", "Regenerate reflection", false)
  );
}

function renderEntry(data, entryId, requestedTab) {
  var entry = findEntry(data, entryId);
  if (!entry) {
    return renderProduct(
      "entry",
      '<div class="product-entry-layout"><article class="product-entry-record">' +
        renderPageHeading("Entry", "No entry available") +
        "</article></div>",
      "product-page--padded"
    );
  }
  var tab = ["body", "reflection", "conversation"].indexOf(requestedTab) === -1
    ? "reflection"
    : requestedTab;
  var tabs = [
    {key: "body", label: "Body"},
    {key: "reflection", label: "Reflection"},
    {key: "conversation", label: "Conversation"},
  ]
    .map(function (item) {
      var selected = item.key === tab;
      return (
        '<button type="button" class="product-entry-tab' +
        (selected ? " is-active" : "") +
        '" data-entry-tab="' +
        item.key +
        '" role="tab" aria-selected="' +
        (selected ? "true" : "false") +
        '">' +
        item.label +
        "</button>"
      );
    })
    .join("");
  var metadata =
    '<p class="product-record-metadata"><time datetime="' +
    escapeHtml(entry.date) +
    '">' +
    escapeHtml(entry.date) +
    " 08:42:16</time><span>·</span><span>" +
    wordCount(entry.body) +
    " words</span><span>·</span><span class=\"product-score\">Wellbeing 78/100</span></p>";
  var content =
    '<div class="product-entry-layout"><article class="product-entry-record">' +
    '<header class="product-entry-heading"><h1>' +
    escapeHtml(entry.title) +
    "</h1>" +
    metadata +
    "</header>" +
    '<div class="product-entry-tabs" role="tablist" aria-label="Entry sections">' +
    tabs +
    '</div><section class="product-entry-panel" role="tabpanel">' +
    renderEntryPanel(entry, tab) +
    "</section></article></div>";
  return renderProduct("entry", content, "product-page--padded");
}

function renderEvidence(section, byId) {
  var items = (section.evidence || [])
    .map(function (ref) {
      var entry = byId[ref];
      if (!entry) return "";
      return (
        '<li><time datetime="' +
        escapeHtml(entry.date) +
        '">' +
        escapeHtml(entry.date) +
        "</time><span>" +
        escapeHtml(entry.title) +
        "</span></li>"
      );
    })
    .join("");
  return items ? '<ul class="product-evidence">' + items + "</ul>" : "";
}

function renderReport(data) {
  var report = (data && data.report) || {sections: []};
  var entries = (data && data.entries) || [];
  var byId = {};
  entries.forEach(function (entry) {
    byId[entry.id] = entry;
  });
  var sections = (report.sections || [])
    .map(function (section) {
      return (
        '<section><h3>' +
        escapeHtml(section.heading) +
        "</h3><p>" +
        escapeHtml(section.body) +
        "</p>" +
        renderEvidence(section, byId) +
        "</section>"
      );
    })
    .join("");
  var density = yearCounts(entries)
    .map(function (item) {
      var width = Math.max(22, Math.round((item.count / Math.max(1, entries.length)) * 420));
      return (
        '<li><span>' +
        item.year +
        '</span><span class="product-density-bar"><span style="width:' +
        width +
        '%"></span></span><span>' +
        item.count +
        "</span></li>"
      );
    })
    .join("");
  var toc = (report.sections || [])
    .map(function (section) {
      return "<span>" + escapeHtml(section.heading) + "</span>";
    })
    .join("");
  var content =
    '<div class="product-report-layout"><article class="product-report-document">' +
    '<header class="product-report-heading"><div class="product-report-heading-row"><h1>Life Report</h1>' +
    renderLockedButton("report", "Generate report", true) +
    '</div><p class="product-report-coverage">Covers ' +
    entries.length +
    " entries · " +
    escapeHtml(entries[0] ? entries[0].date : "") +
    " — " +
    escapeHtml(entries.length ? entries[entries.length - 1].date : "") +
    ' · 0 behind</p><div class="product-report-stats"><div><strong>' +
    entries.length +
    "</strong><span>Entries analyzed</span></div><div><strong>3</strong><span>Reports created</span></div><div><strong>0</strong><span>Entries behind</span></div></div></header>" +
    '<div class="product-perspective">Perspective: Analyst</div><div class="product-report-prose"><h2>' +
    escapeHtml(report.title || "Life Report") +
    "</h2>" +
    sections +
    '</div></article><aside class="product-report-rail"><section><h2>Entries by year</h2><ul class="product-density-list">' +
    density +
    '</ul></section><section><h2>History</h2><ol class="product-report-versions"><li class="is-active"><span></span><strong>V03</strong><time>2024-02-22</time><em>' +
    entries.length +
    '</em></li><li><span></span><strong>V02</strong><time>2023-11-03</time><em>5</em></li><li><span></span><strong>V01</strong><time>2023-04-12</time><em>4</em></li></ol></section><nav class="product-report-toc" aria-label="Report contents">' +
    toc +
    "</nav></aside></div>";
  return renderProduct("report", content, "product-page--padded");
}

function renderConversation(data) {
  var conversation = (data && data.conversation) || {messages: []};
  var sessions = [
    {title: conversation.title || "Continuing the reflection", date: "2024-02-22", count: 4},
    {title: "What counts as enough evidence to choose?", date: "2023-10-16", count: 6},
    {title: "Looking back at the workshop lease", date: "2023-03-10", count: 3},
  ];
  var sessionRows = sessions
    .map(function (session, index) {
      return (
        '<li class="product-session-row' +
        (index === 0 ? " is-active" : "") +
        '"><button type="button" disabled><span class="product-session-title">' +
        escapeHtml(session.title) +
        '</span><span class="product-session-meta">' +
        escapeHtml(session.date) +
        " · " +
        session.count +
        " messages</span></button></li>"
      );
    })
    .join("");
  var messages = (conversation.messages || [])
    .map(function (message, index) {
      var role = message.role === "assistant" ? "Mentor" : "Me";
      return (
        '<article class="product-message product-message--' +
        escapeHtml(message.role) +
        '"><div class="product-message-stamp"><span>' +
        role +
        "</span><time>09:" +
        String(21 + index).padStart(2, "0") +
        "</time></div><p>" +
        escapeHtml(message.text) +
        "</p></article>"
      );
    })
    .join("");
  var content =
    '<div class="product-chat-layout"><aside class="product-session-ledger"><div class="product-session-heading"><h1>Conversations</h1><button type="button" disabled>New</button></div><ol>' +
    sessionRows +
    '</ol></aside><section class="product-conversation-workspace"><header class="product-conversation-heading"><h1>' +
    escapeHtml(conversation.title || "Conversation") +
    '</h1><p>Challenge the reading or follow a thread across the fictional archive.</p></header><div class="product-conversation-thread">' +
    messages +
    '</div><div class="product-conversation-dock"><div class="product-perspective">Next response: Analyst</div><div class="product-composer"><textarea aria-label="Conversation input" placeholder="Ask a follow-up question" readonly></textarea><button type="button" disabled>Send</button></div><span class="product-lock-note">' +
    SELF_HOSTED_NOTICE +
    "</span></div></section></div>";
  return renderProduct("conversation", content, "product-page--chat");
}

function renderWrite() {
  var content =
    '<section class="product-writing-frame"><form class="product-writing-desk"><header class="product-writing-ledger"><div><label>Date</label><input type="date" value="2024-03-02" readonly><span>Saturday</span><p>Last entry 10 days ago</p></div><span class="product-draft-status">Sample editor</span></header><div class="product-writing-editor"><input class="product-writing-title" type="text" aria-label="Entry title" placeholder="Title" readonly><textarea class="product-writing-body" aria-label="Entry body" placeholder="What do you want to remember?" readonly></textarea><p class="product-writing-count">0 words</p></div>' +
    renderLockedButton("write", "Save entry", true) +
    "</form></section>";
  return renderProduct("write", content, "product-page--padded");
}

function renderWorkshop(data) {
  var workshop = (data && data.workshop) || {perspectives: []};
  var perspectives = workshop.perspectives || [];
  var active = null;
  for (var i = 0; i < perspectives.length; i++) {
    if (perspectives[i].key === "analyst") active = perspectives[i];
  }
  active = active || perspectives[0] || {name: "Analyst", instructions: "", reading: ""};
  var entry = findEntry(data, workshop.entry_id);
  var options = perspectives
    .map(function (perspective) {
      var checked = perspective.key === (active.key || "analyst");
      return (
        '<label class="product-perspective-option' +
        (checked ? " is-active" : "") +
        '"><input type="radio" name="perspective" disabled' +
        (checked ? " checked" : "") +
        "><span><strong>" +
        escapeHtml(perspective.name) +
        "</strong><small>" +
        escapeHtml(
          perspective.key === "companion"
            ? "Warm first, then widen the reading."
            : perspective.key === "coach"
              ? "Connect patterns to supported next steps."
              : perspective.key === "challenger"
                ? "Name contradiction without attacking identity."
                : perspective.key === "custom"
                  ? "Keep your own saved instructions."
                  : "Separate observation from interpretation."
        ) +
        "</small></span></label>"
      );
    })
    .join("");
  var content =
    renderPageHeading("Prompt Workshop", "Choose how future reflections read the archive") +
    '<div class="product-workshop-layout"><aside class="product-workshop-side"><section><h2>Perspective</h2><div class="product-perspective-options">' +
    options +
    '</div></section><section><label>Model<select disabled><option>GPT-5.4</option></select></label><label>Language<select disabled><option>Match journal</option></select></label></section></aside><section class="product-workshop-main"><header><div><h2>Instructions</h2><p>Active Perspective: ' +
    escapeHtml(active.name) +
    '</p></div><span>Draft</span></header><textarea class="product-workshop-editor" readonly>' +
    escapeHtml(active.instructions) +
    '</textarea><section class="product-workshop-test"><div><label>Preview entry</label><select disabled><option>' +
    escapeHtml(entry ? entry.date + " · " + entry.title : "No entry") +
    '</option></select></div><button type="button" disabled>Run preview</button></section><div class="product-preview"><div class="product-perspective">Preview: ' +
    escapeHtml(active.name) +
    "</div><p>" +
    escapeHtml(active.reading) +
    "</p></div></section><footer class=\"product-workshop-actionbar\">" +
    renderLockedButton("workshop", "Apply and regenerate all", true) +
    "<p>Applying changes affects future responses. Existing versions stay unchanged.</p></footer></div>";
  return renderProduct("workshop", content, "product-page--padded");
}

function renderView(viewKey, data, entryId, entryTab) {
  switch (normalizeView(viewKey)) {
    case "entry":
      return renderEntry(data, entryId, entryTab);
    case "report":
      return renderReport(data);
    case "conversation":
      return renderConversation(data);
    case "write":
      return renderWrite();
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
  var initialView = normalizeView(
    queryView != null ? queryView : rootEl.getAttribute("data-initial-view")
  );
  var state = {
    view: initialView,
    data: null,
    entryId: null,
    entryTab: initialView === "entry" ? "reflection" : "body",
  };

  function selectView(rawView) {
    state.view = normalizeView(rawView);
    if (state.view === "entry") state.entryTab = "reflection";
    paint();
  }

  function paint() {
    buttons.forEach(function (button) {
      var active = normalizeView(button.getAttribute("data-view")) === state.view;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-current", active ? "true" : "false");
    });
    stage.innerHTML = renderView(state.view, state.data, state.entryId, state.entryTab);
    Array.prototype.slice.call(stage.querySelectorAll("[data-product-view]")).forEach(function (button) {
      button.addEventListener("click", function () {
        selectView(button.getAttribute("data-product-view"));
      });
    });
    Array.prototype.slice.call(stage.querySelectorAll("[data-entry-id]")).forEach(function (link) {
      link.addEventListener("click", function () {
        state.entryId = link.getAttribute("data-entry-id");
        state.entryTab = "body";
        state.view = "entry";
        paint();
      });
    });
    Array.prototype.slice.call(stage.querySelectorAll("[data-entry-tab]")).forEach(function (button) {
      button.addEventListener("click", function () {
        state.entryTab = button.getAttribute("data-entry-tab");
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
        selectView(button.getAttribute("data-view"));
      });
    });
    paint();
  } catch (error) {
    stage.innerHTML = renderError() + fallbackHtml;
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", function () {
    if (typeof window !== "undefined" && captureFromQuery(window.location.search)) {
      document.documentElement.classList.add("demo-capture");
    }
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
    captureFromQuery: captureFromQuery,
    parseFixture: parseFixture,
    findEntry: findEntry,
    renderView: renderView,
    renderError: renderError,
    initDemo: initDemo,
  };
}
