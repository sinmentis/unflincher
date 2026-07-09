"""Directly unit-tests the shipped src/diary/static/app.js SSE frame parser (parseSseFrame) via
Node, closing the multi-line streamed-text corruption bug at the exact layer it lives.

The Python-side persistence test in test_routes_entry.py proves the DB stays clean, but the bug
was purely client-side rendering; only running the real JS guards the browser parser itself
against a regression back to the single greedy `data:` capture. Skipped when node is absent."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sse_starlette.sse import ServerSentEvent

APP_JS = Path(__file__).resolve().parents[1] / "src" / "diary" / "static" / "app.js"

# Stub the browser globals app.js touches at load time, then hand a real server frame to the
# actual parseSseFrame and print its result as JSON for the Python side to assert on.
_NODE_HARNESS = (
    "globalThis.document = { body: { addEventListener() {} }, cookie: '' };"
    "const {parseSseFrame} = require(process.argv[1]);"
    "const {ev, data} = parseSseFrame(process.argv[2]);"
    "process.stdout.write(JSON.stringify({ev, data}));"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_app_js_parseSseFrame_rejoins_multiline_data_from_real_server_frame():
    token = "第一行\n第二行\n\n列表：\n- 项目一"
    # The exact wire frame sse-starlette emits: embedded newlines become multiple `data: ` lines.
    frame = ServerSentEvent(data=token, event="token", sep="\n").encode().decode().split("\n\n")[0]
    assert frame.count("data: ") > 1  # sanity: the frame really is multi-`data:`-line

    out = subprocess.run(
        ["node", "-e", _NODE_HARNESS, str(APP_JS), frame],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = json.loads(out)

    assert result["ev"] == "token"
    assert result["data"] == token  # multi-line token reconstructed exactly
    assert "data: " not in result["data"]  # no stray SSE field prefix leaked into rendered text


# Regression test for a real production bug: the CSS-only "disable while streaming" treatment
# (main:has([data-streaming="1"]) #trigger { pointer-events: none }) only blocks MOUSE clicks --
# a keyboard Enter/Space on the still-focused trigger button, or any programmatic .click(), was
# NOT blocked, so a second streamInto() call on the same target while the first was still reading
# its response raced the first: both loops appended tokens to the same element concurrently, and
# the second call's own textContent="" wiped whatever the first had written so far, producing a
# spliced/corrupted result. Reproduced live (mouse click blocked as expected; a focused-button
# Enter key fired a second POST /workshop/test-run while the first was mid-stream). The fix moves
# the guard into streamInto itself: a call on an already-streaming target is a no-op, regardless of
# how it was triggered.
_REENTRANCY_NODE_HARNESS = """
globalThis.document = { body: { addEventListener() {} }, cookie: '' };
const {streamInto} = require(process.argv[1]);

let fetchCalls = 0;
globalThis.fetch = async () => {
  fetchCalls++;
  const chunks = ['event: token\\ndata: hi\\n\\n', 'event: done\\ndata: {}\\n\\n'];
  let i = 0;
  return {
    body: {
      getReader() {
        return {
          async read() {
            if (i < chunks.length) {
              return {value: new TextEncoder().encode(chunks[i++]), done: false};
            }
            return {value: undefined, done: true};
          },
        };
      },
    },
  };
};

const target = {dataset: {streaming: '1'}, textContent: 'existing content', style: {}};
streamInto('/x', null, target).then(() => {
  process.stdout.write(JSON.stringify({fetchCalls, textContent: target.textContent}));
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_stream_into_ignores_reinvocation_on_an_already_streaming_target():
    out = subprocess.run(
        ["node", "-e", _REENTRANCY_NODE_HARNESS, str(APP_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = json.loads(out)

    # No fetch at all: the guard returns before ever starting a second request.
    assert result["fetchCalls"] == 0
    # And critically, the pre-existing (first stream's) content is left untouched -- the bug's
    # exact symptom was this being wiped by the second call's textContent = "".
    assert result["textContent"] == "existing content"


_ALLOWS_FRESH_STREAM_NODE_HARNESS = """
globalThis.document = { body: { addEventListener() {} }, cookie: '' };
const {streamInto} = require(process.argv[1]);

let fetchCalls = 0;
globalThis.fetch = async () => {
  fetchCalls++;
  const chunks = ['event: token\\ndata: hi\\n\\n', 'event: done\\ndata: {}\\n\\n'];
  let i = 0;
  return {
    body: {
      getReader() {
        return {
          async read() {
            if (i < chunks.length) {
              return {value: new TextEncoder().encode(chunks[i++]), done: false};
            }
            return {value: undefined, done: true};
          },
        };
      },
    },
  };
};

// Not currently streaming (no dataset.streaming key at all) -- a normal, non-overlapping call.
const target = {dataset: {}, textContent: 'stale text from a previous run', style: {}};
streamInto('/x', null, target).then(() => {
  process.stdout.write(JSON.stringify({fetchCalls, textContent: target.textContent, streaming: target.dataset.streaming}));
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_stream_into_still_runs_normally_when_target_is_not_already_streaming():
    out = subprocess.run(
        ["node", "-e", _ALLOWS_FRESH_STREAM_NODE_HARNESS, str(APP_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = json.loads(out)

    assert result["fetchCalls"] == 1
    assert result["textContent"] == "hi"  # cleared, then the new stream's own token
    assert "streaming" not in result  # dataset.streaming was deleted after completion, not set to null
