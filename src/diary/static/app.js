// src/diary/static/app.js — shared SSE-consumer, reused by Task 10 (chat) and Task 14 (test-run)
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

async function streamInto(url, body, targetEl, onDone) {
  // Re-entrancy guard: the CSS-only "disable while streaming" treatment
  // (main:has([data-streaming="1"]) #trigger-btn { pointer-events: none }) only blocks MOUSE
  // clicks -- it does nothing against a keyboard Enter/Space on the still-focused button, or any
  // programmatic .click(). Without this check, a second invocation while the first is still
  // reading its response body races the first: both loops end up appending tokens to the same
  // targetEl concurrently, and this call's own `textContent = ""` below wipes whatever the first
  // stream had already written, producing corrupted, spliced-together output. Ignoring a
  // re-invocation while already streaming is the actual fix; the CSS is just a visual hint.
  if (targetEl.dataset.streaming === "1") return;
  targetEl.style.display = "block";
  targetEl.textContent = "";
  targetEl.dataset.streaming = "1";
  const res = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": getCsrfToken()},
    body: body ? JSON.stringify(body) : undefined,
  });
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const {value, done} = await reader.read();
    if (done) break;
    buf += dec.decode(value, {stream: true});
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const {ev, data} = parseSseFrame(frame);
      if (ev === "token") targetEl.textContent += data;
      else if (ev === "error") targetEl.insertAdjacentHTML("beforeend", '<span class="stream-err">生成中断</span>');
      else if (ev === "done") {
        // Persisted surfaces (entry commentary/chat, general chat, report) call
        // location.reload() from their onDone callback, which replaces this whole element with a
        // freshly server-rendered one anyway. The workshop test-run preview is the one caller that
        // never reloads (it must never persist), so it needs its OWN final render pass here: if the
        // server sent rendered html (see routes/workshop.py), swap it in now, while the target is
        // still marked data-streaming="1" -- this happens strictly before that attribute is
        // cleared below, so there is no frame where the raw pre-wrap text is shown without the
        // benefit of white-space:pre-wrap (which is what previously caused the finished preview to
        // visually collapse: newlines were only preserved while data-streaming="1" was set, and
        // nothing ever put the text into real HTML block elements once streaming ended).
        let payload = {};
        try {
          payload = JSON.parse(data);
        } catch {
          // Other routes' done payload is the literal string "{}" too, so this never actually
          // throws in practice; the try/catch only guards against a malformed frame.
        }
        if (payload.html) targetEl.innerHTML = payload.html;
        onDone?.(payload);
      }
    }
  }
  delete targetEl.dataset.streaming;
}

// Exposed for Node-based unit testing; harmless in the browser where `module` is undefined.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {parseSseFrame, streamInto};
}
