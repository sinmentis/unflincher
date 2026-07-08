// src/diary/static/app.js — shared SSE-consumer, reused by Task 10 (chat) and Task 14 (test-run)
async function streamInto(url, body, targetEl, onDone) {
  targetEl.dataset.streaming = "1";
  const res = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
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
      const ev = /event: (.*)/.exec(frame)?.[1] || "token";
      const data = /data: ([\s\S]*)/.exec(frame)?.[1] ?? "";
      if (ev === "token") targetEl.textContent += data;
      else if (ev === "error") targetEl.insertAdjacentHTML("beforeend", '<span class="stream-err">生成中断</span>');
      else if (ev === "done") onDone?.(data);
    }
  }
  delete targetEl.dataset.streaming;
}
