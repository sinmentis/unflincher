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
      else if (ev === "done") onDone?.(data);
    }
  }
  delete targetEl.dataset.streaming;
}

// Exposed for Node-based unit testing; harmless in the browser where `module` is undefined.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {parseSseFrame};
}
