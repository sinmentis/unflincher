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
    if (res.ok === false || !res.body) throw new Error(`stream request failed: ${res.status}`);
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
    errorNode.textContent = UI_MESSAGES.streamInterrupted || UI_MESSAGES.requestFailed || "";
    targetEl.append(errorNode);
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
