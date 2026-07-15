// src/unflincher/static/app.js — shared SSE-consumer, reused by Task 10 (chat) and Task 14 (test-run)

function getCsrfToken() {
  const match = document.cookie.match(/(?:^|; )csrf_token=([^;]+)/);
  return match ? match[1] : "";
}

document.body.addEventListener("htmx:configRequest", (event) => {
  event.detail.headers["X-CSRF-Token"] = getCsrfToken();
});

// Per the SSE spec a data payload containing newlines is serialized as MULTIPLE `data: ` lines
// inside one event frame (sse-starlette does exactly this). Collect every `data: ` line and
// rejoin them with "\n" instead of a single greedy capture, which would otherwise embed literal
// "data: " fragments into multi-line streamed text.
function parseSseFrame(frame) {
  const lines = frame.split("\n");
  const ev = (lines.find((l) => l.startsWith("event: ")) || "event: token").slice(7);
  const data = lines
    .filter((l) => l.startsWith("data: "))
    .map((l) => l.slice(6))
    .join("\n");
  return {ev, data};
}

// Carries the stable generation-safety JSON body (see routes/errors.py's
// generation_safety_http_exception) alongside the HTTP status, so the catch block in streamInto
// can render a specific, actionable notice for reasons it recognizes (currently
// "context_too_large") while falling through to the existing generic notice for everything else
// (network errors, unrecognized/non-JSON error bodies, and mid-stream `error` SSE events).
class StreamRequestError extends Error {
  constructor(status, detail) {
    super(`stream request failed: ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

// Reads FastAPI's `detail` object out of an already-parsed JSON response body if it has the
// stable `{reason: "..."}` shape this app's routes always use for generation-safety errors (see
// routes/errors.py). Returns null for anything else -- callers must treat null as "unrecognized
// failure", never guess a reason. Synchronous and reusable from both a `fetch` Response body
// (already parsed) and an htmx XHR's responseText (parsed separately, see
// parseStableErrorDetailFromText) -- ONE shape-check, not two copies that could drift.
function extractStableErrorDetail(parsedBody) {
  if (
    parsedBody
    && typeof parsedBody.detail === "object"
    && parsedBody.detail
    && typeof parsedBody.detail.reason === "string"
  ) {
    return parsedBody.detail;
  }
  return null;
}

// Reads the failed response's JSON body once and returns the stable detail, or null. Used by
// every plain `fetch()`-based caller (streamInto, entry.js's single-entry trigger, workshop.js's
// apply-all) so the exact same parsing logic backs all of them.
async function parseStableErrorDetail(res) {
  try {
    return extractStableErrorDetail(await res.json());
  } catch {
    // Not JSON (or no body) -- fall through to null.
  }
  return null;
}

// Same parse, but for htmx's XHR-based `htmx:responseError` event, whose body is already
// available synchronously as `event.detail.xhr.responseText` rather than a fetch Response to
// await .json() on.
function parseStableErrorDetailFromText(text) {
  try {
    return extractStableErrorDetail(JSON.parse(text));
  } catch {
    return null;
  }
}

// Fills the two dynamic numbers into the localized "{estimated} > {limit}" template. Plain
// string substitution, not a templating library, matching this app's dependency-free JS.
function contextTooLargeMessage(detail) {
  const template = UI_MESSAGES.contextTooLarge || "";
  return template
    .replace("{estimated}", detail.estimated_tokens)
    .replace("{limit}", detail.limit);
}

// One shared renderer for the small single-line `.notice` divs used outside streamInto's own
// (larger, two-paragraph) rendering: entry.js's single-entry commentary trigger, workshop.js's
// apply/apply-all/test-run, and the htmx retry handler below. Combines the capacity message with
// its actions into ONE string (these notices are plain text nodes, see setNotice) for
// context_too_large; a handful of OTHER Workshop typed-contract reasons (Task: Workshop) get
// their own short localized message; anything else (an unrecognized reason, or no parseable
// detail at all) falls through to `fallbackMessage`, preserving each caller's exact previous
// generic-failure text.
function stableErrorNoticeMessage(detail, fallbackMessage) {
  if (!detail) return fallbackMessage;
  if (detail.reason === "context_too_large") {
    return `${contextTooLargeMessage(detail)} ${UI_MESSAGES.contextTooLargeActions || ""}`.trim();
  }
  if (detail.reason === "unsupported_model") {
    return UI_MESSAGES.unsupportedModel || fallbackMessage;
  }
  if (detail.reason === "model_limits_unavailable") {
    return UI_MESSAGES.modelCatalogOutage || fallbackMessage;
  }
  if (detail.reason === "empty_instructions") {
    return UI_MESSAGES.emptyInstructions || fallbackMessage;
  }
  return fallbackMessage;
}

// Targeted htmx error handler for the ONE htmx-driven generation-adjacent action in this app:
// the failed-job-item retry button in partials/job_progress.html (hx-post .../retry). Scoped to
// elements explicitly marked `data-generation-retry` so this never fires for this app's other,
// unrelated htmx requests (job-progress polling, commentary-status polling). Renders into
// #workshop-notice, the page-level notice element already present alongside the htmx-swapped
// #regen-progress region on workshop.html.
document.body.addEventListener("htmx:responseError", (event) => {
  const elt = event.detail && event.detail.elt;
  if (!elt || typeof elt.matches !== "function" || !elt.matches("[data-generation-retry]")) return;
  const notice = document.getElementById("workshop-notice");
  if (!notice) return;
  const detail = parseStableErrorDetailFromText(event.detail.xhr ? event.detail.xhr.responseText : "");
  setNotice(notice, stableErrorNoticeMessage(detail, UI_MESSAGES.requestFailed), "failed");
});

async function streamInto(url, body, targetEl, onDone, onError) {
  // Re-entrancy guard: the CSS-only "disable while streaming" treatment
  // (main:has([data-streaming="1"]) #trigger-btn { pointer-events: none }) only blocks MOUSE
  // clicks -- it does nothing against a keyboard Enter/Space on the still-focused button, or any
  // programmatic .click(). Without this check, a second invocation while the first is still
  // reading its response body races the first: both loops end up appending tokens to the same
  // targetEl concurrently, and this call's own `textContent = ""` below wipes whatever the first
  // stream had already written, producing corrupted, spliced-together output. Ignoring a
  // re-invocation while already streaming is the actual fix; the CSS is just a visual hint.
  if (targetEl.dataset.streaming === "1") return;
  targetEl.hidden = false;
  targetEl.style.display = "block";
  targetEl.textContent = "";
  targetEl.dataset.streaming = "1";
  targetEl.dataset.streamState = "running";
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": getCsrfToken()},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.ok === false || !res.body) {
      // Read the JSON body BEFORE any SSE handling -- a failed preflight (413/503/409) never
      // opens an SSE stream at all, so this is the only chance to see the stable detail.
      const detail = res.ok === false ? await parseStableErrorDetail(res) : null;
      throw new StreamRequestError(res.status, detail);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let completed = false;
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const {ev, data} = parseSseFrame(frame);
        if (ev === "token") {
          targetEl.textContent += data;
        } else if (ev === "error") {
          throw new Error("server ended the stream with an error event");
        } else if (ev === "done") {
          // Persisted surfaces (entry commentary/chat, general chat, report) call
          // location.reload() from their onDone callback, which replaces this whole element with a
          // freshly server-rendered one anyway. The workshop test-run preview is the one caller
          // that never reloads (it must never persist), so it needs its OWN final render pass here:
          // if the server sent rendered html (see routes/workshop.py), swap it in now, while the
          // target is still marked data-streaming="1" -- strictly before that attribute is cleared
          // below, so there is no frame where the raw pre-wrap text is shown without the benefit of
          // white-space:pre-wrap.
          let payload = {};
          try {
            payload = JSON.parse(data);
          } catch {
            payload = {};
          }
          if (payload.html) targetEl.innerHTML = payload.html;
          completed = true;
          onDone?.(payload);
        }
      }
    }
    if (!completed) throw new Error("stream ended before the done event");
    targetEl.dataset.streamState = "done";
  } catch (error) {
    // Failed HTTP, network drop, a server `error` event, or premature EOF all land here. Keep
    // whatever partial tokens already streamed in, expose the failed state for CSS/tests, append
    // an accessible localized failure notice, and hand the error to the caller.
    targetEl.dataset.streamState = "failed";
    const errorNode = document.createElement("p");
    errorNode.className = "notice notice--failed";
    if (error instanceof StreamRequestError && error.detail?.reason === "context_too_large") {
      // Plan lines 174-175: the owner must see both the estimated size and the model's limit,
      // plus actionable next steps -- a generic "interrupted" notice loses that detail.
      errorNode.textContent = contextTooLargeMessage(error.detail);
      const actionsNode = document.createElement("p");
      actionsNode.className = "notice notice--failed-actions";
      actionsNode.textContent = UI_MESSAGES.contextTooLargeActions || "";
      targetEl.append(errorNode, actionsNode);
    } else {
      // Any OTHER stable typed-contract reason (Workshop's unsupported model / catalog outage /
      // empty instructions) gets its own short localized message via the SAME shared renderer
      // used by the plain-fetch callers below; anything unrecognized (or a network error) keeps
      // the existing generic "stream interrupted" text.
      const fallback = UI_MESSAGES.streamInterrupted || UI_MESSAGES.requestFailed || "";
      errorNode.textContent = error instanceof StreamRequestError
        ? stableErrorNoticeMessage(error.detail, fallback)
        : fallback;
      targetEl.append(errorNode);
    }
    onError?.(error);
  } finally {
    // Always clear the re-entrancy flag, success or failure, so the surface can be retried.
    delete targetEl.dataset.streaming;
  }
}

// New-entry draft autosave (see docs/superpowers/specs/2026-07-10-diary-new-entry-draft-autosave-
// design.md). Pure functions taking a `storage` argument (rather than touching window.localStorage
// directly) so they're unit-testable from Node with a plain in-memory fake, matching parseSseFrame's
// testing pattern above. There is only ever one "in-progress new entry" at a time (a single,
// unparameterized /new route), so one fixed key is sufficient -- no per-session namespacing needed.
const DRAFT_KEY = "diary-new-entry-draft";

function saveDraft(storage, draft) {
  storage.setItem(DRAFT_KEY, JSON.stringify(draft));
}

// Returns null if there's no stored draft, the stored JSON is malformed, or every field is
// empty (an all-empty draft is indistinguishable from "no draft" and must not override the
// date field's own "today" default with a blank).
function loadDraft(storage) {
  const raw = storage.getItem(DRAFT_KEY);
  if (!raw) return null;
  let draft;
  try {
    draft = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!draft.date && !draft.title && !draft.content) return null;
  return draft;
}

function clearDraft(storage) {
  storage.removeItem(DRAFT_KEY);
}

// Shared editorial UI primitives (Task 3). UI_MESSAGES replaces Task 2's temporary window.I18N
// shim: it is read once from the base document's #ui-messages JSON at load. readUiMessages is
// exported too so callers/tests can re-read from an explicit document.
function readUiMessages(doc = document) {
  if (!doc || typeof doc.getElementById !== "function") return {};
  const node = doc.getElementById("ui-messages");
  if (!node) return {};
  try {
    return JSON.parse(node.textContent || "{}");
  } catch {
    return {};
  }
}

const UI_MESSAGES = typeof document === "undefined" ? {} : readUiMessages(document);

// Expose UI_MESSAGES to page-specific modules (entry.js) that read localized notices from a
// browser global. Guarded so Node-based unit tests (where `window` is undefined) stay unaffected.
if (typeof window !== "undefined") {
  window.UI_MESSAGES = UI_MESSAGES;
}

function setNotice(element, message, tone = "info") {
  element.textContent = message;
  element.dataset.tone = tone;
  element.hidden = false;
}

function clearNotice(element) {
  element.textContent = "";
  delete element.dataset.tone;
  element.hidden = true;
}

// Enter submits, Shift+Enter inserts a newline, and an IME composition keystroke never submits.
function shouldSubmitComposer(event) {
  return event.key === "Enter" && !event.shiftKey && !event.isComposing;
}

// Wires an auto-growing chat composer: the textarea grows to a 192px cap, Enter submits via the
// form, and the single-flight `data-busy` guard blocks a duplicate turn while onSubmit is pending.
function bindComposer(form, onSubmit) {
  const input = form.querySelector("textarea");
  const submit = form.querySelector('button[type="submit"]');
  const resize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 192)}px`;
  };
  input.addEventListener("input", resize);
  input.addEventListener("keydown", (event) => {
    if (!shouldSubmitComposer(event)) return;
    event.preventDefault();
    form.requestSubmit();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (form.dataset.busy === "1") return;
    const message = input.value.trim();
    if (!message) return;
    form.dataset.busy = "1";
    submit.disabled = true;
    try {
      await onSubmit(message);
    } finally {
      delete form.dataset.busy;
      submit.disabled = false;
    }
  });
}

// Exposed for Node-based unit testing; harmless in the browser where `module` is undefined.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    parseSseFrame,
    streamInto,
    StreamRequestError,
    extractStableErrorDetail,
    parseStableErrorDetail,
    parseStableErrorDetailFromText,
    contextTooLargeMessage,
    stableErrorNoticeMessage,
    saveDraft,
    loadDraft,
    clearDraft,
    DRAFT_KEY,
    readUiMessages,
    setNotice,
    clearNotice,
    shouldSubmitComposer,
    bindComposer,
  };
}
