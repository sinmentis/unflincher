"""Directly unit-tests the shipped src/unflincher/static/app.js SSE frame parser (parseSseFrame) via
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

APP_JS = Path(__file__).resolve().parents[1] / "src" / "unflincher" / "static" / "app.js"

# Stub the browser globals app.js touches at load time, then hand a real server frame to the
# actual parseSseFrame and print its result as JSON for the Python side to assert on.
_NODE_HARNESS = (
    "globalThis.document = { body: { addEventListener() {} }, cookie: '' };"
    "const {parseSseFrame} = require(process.argv[1]);"
    "const {ev, data} = parseSseFrame(process.argv[2]);"
    "process.stdout.write(JSON.stringify({ev, data}));"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_keepStreamVisible_follows_the_tail_without_fighting_manual_scroll():
    script = """
    globalThis.document = { body: { addEventListener() {} }, cookie: '' };
    const {keepStreamVisible} = require(process.argv[1]);
    const container = {scrollHeight: 500, scrollTop: 350, clientHeight: 100};
    const target = {closest() { return container; }};

    keepStreamVisible(target);
    const followed = container.scrollTop;
    container.scrollTop = 100;
    keepStreamVisible(target);
    const preserved = container.scrollTop;
    keepStreamVisible(target, {force: true});

    process.stdout.write(JSON.stringify({followed, preserved, forced: container.scrollTop}));
    """
    output = subprocess.run(
        ["node", "-e", script, str(APP_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert json.loads(output) == {"followed": 500, "preserved": 100, "forced": 500}


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


# Regression test for a second real production bug found alongside the re-entrancy one: the
# workshop test-run preview never reloads the page (unlike entry commentary/chat, general chat,
# and the report page, all of which call location.reload() from onDone and get a fresh
# server-rendered pass) -- so its target element used to sit forever holding raw markdown-source
# plaintext, and once data-streaming="1" was removed the CSS white-space:pre-wrap rule tied to
# that attribute stopped applying, visually collapsing every paragraph break/newline the model had
# written. The fix (routes/workshop.py) sends real rendered HTML back in the `done` event's JSON
# payload; this test proves the client swaps it in via innerHTML, and does so BEFORE
# dataset.streaming is cleared (so there is no frame where the raw, now-unstyled text is visible).
_HTML_SWAP_ON_DONE_NODE_HARNESS = """
globalThis.document = { body: { addEventListener() {} }, cookie: '' };
const {streamInto} = require(process.argv[1]);

globalThis.fetch = async () => {
  const chunks = [
    'event: token\\ndata: **hi**\\n\\n',
    'event: done\\ndata: {"html":"<p><strong>hi</strong></p>"}\\n\\n',
  ];
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

const target = {dataset: {}, textContent: '', innerHTML: '', style: {}};
let onDonePayload = null;
streamInto('/x', null, target, (payload) => { onDonePayload = payload; }).then(() => {
  process.stdout.write(JSON.stringify({
    innerHTML: target.innerHTML,
    textContent: target.textContent,
    streaming: target.dataset.streaming,
    onDonePayload,
  }));
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_stream_into_swaps_in_rendered_html_from_the_done_event_payload():
    out = subprocess.run(
        ["node", "-e", _HTML_SWAP_ON_DONE_NODE_HARNESS, str(APP_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = json.loads(out)

    # The raw "**hi**" token text is replaced by the server-rendered HTML -- no literal markdown
    # source left behind, and no reliance on white-space:pre-wrap to look right. (innerHTML is the
    # one property real DOM and this plain-object test double agree on here; textContent isn't
    # asserted post-swap since a real element's textContent getter recomputes from its live DOM
    # tree after an innerHTML write, which a plain mock object can't reproduce.)
    assert result["innerHTML"] == "<p><strong>hi</strong></p>"
    assert "streaming" not in result  # cleared after completion, same as every other done path
    assert result["onDonePayload"] == {"html": "<p><strong>hi</strong></p>"}



_DRAFT_NODE_HARNESS = (
    "globalThis.document = { body: { addEventListener() {} }, cookie: '' };"
    "const {saveDraft, loadDraft, clearDraft, DRAFT_KEY} = require(process.argv[1]);"
    # A minimal in-memory fake standing in for window.localStorage -- exercises the real
    # save/load/clear functions without needing a jsdom/browser localStorage polyfill in Node.
    "const store = {};"
    "const fakeStorage = {"
    "  setItem(k, v) { store[k] = v; },"
    "  getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },"
    "  removeItem(k) { delete store[k]; },"
    "};"
    "const results = {};"
    ""
    "results.loadWithNoDraft = loadDraft(fakeStorage);"
    ""
    "saveDraft(fakeStorage, {date: '2026-01-01', title: 't', content: 'c'});"
    "results.rawAfterSave = store[DRAFT_KEY];"
    "results.loadAfterSave = loadDraft(fakeStorage);"
    ""
    "clearDraft(fakeStorage);"
    "results.loadAfterClear = loadDraft(fakeStorage);"
    ""
    "saveDraft(fakeStorage, {date: '', title: '', content: ''});"
    "results.loadAllEmptyDraft = loadDraft(fakeStorage);"
    ""
    "store[DRAFT_KEY] = 'not valid json {{{';"
    "results.loadMalformedDraft = loadDraft(fakeStorage);"
    ""
    "process.stdout.write(JSON.stringify(results));"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_draft_save_load_clear_round_trip():
    out = subprocess.run(
        ["node", "-e", _DRAFT_NODE_HARNESS, str(APP_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = json.loads(out)

    assert result["loadWithNoDraft"] is None
    assert result["rawAfterSave"] == '{"date":"2026-01-01","title":"t","content":"c"}'
    assert result["loadAfterSave"] == {"date": "2026-01-01", "title": "t", "content": "c"}
    assert result["loadAfterClear"] is None
    # An all-fields-empty draft is indistinguishable from "no draft" -- must not override the
    # date field's own "today" default with a blank on page load.
    assert result["loadAllEmptyDraft"] is None
    # Malformed JSON in storage (e.g. from a future format change) must not throw -- it's
    # treated the same as "no usable draft", not a page-breaking error.
    assert result["loadMalformedDraft"] is None


_UI_PRIMITIVES_HARNESS = """
globalThis.document = {
  body: { addEventListener() {} },
  cookie: '',
  getElementById(id) {
    if (id !== 'ui-messages') return null;
    return {textContent: '{"working":"Working…","busy":"Busy"}'};
  },
};
const {readUiMessages, setNotice, clearNotice, shouldSubmitComposer} = require(process.argv[1]);
const notice = {hidden: true, textContent: '', dataset: {}};
setNotice(notice, 'Busy', 'busy');
const shown = {hidden: notice.hidden, text: notice.textContent, tone: notice.dataset.tone};
clearNotice(notice);
process.stdout.write(JSON.stringify({
  messages: readUiMessages(document),
  shown,
  cleared: {hidden: notice.hidden, text: notice.textContent, tone: notice.dataset.tone},
  enter: shouldSubmitComposer({key: 'Enter', shiftKey: false, isComposing: false}),
  shiftEnter: shouldSubmitComposer({key: 'Enter', shiftKey: true, isComposing: false}),
  composing: shouldSubmitComposer({key: 'Enter', shiftKey: false, isComposing: true}),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_shared_ui_primitives():
    out = subprocess.run(
        ["node", "-e", _UI_PRIMITIVES_HARNESS, str(APP_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = json.loads(out)
    assert result["messages"]["working"] == "Working…"
    assert result["shown"] == {"hidden": False, "text": "Busy", "tone": "busy"}
    assert result["cleared"] == {"hidden": True, "text": ""}
    assert result["enter"] is True
    assert result["shiftEnter"] is False
    assert result["composing"] is False


_STREAM_ERROR_HARNESS = """
globalThis.document = {
  body: {addEventListener() {}},
  cookie: '',
  getElementById() {
    return {textContent: '{"streamInterrupted":"Generation interrupted"}'};
  },
  createElement() {
    return {className: '', textContent: ''};
  },
};
globalThis.fetch = async () => {
  const chunks = [
    'event: token\\ndata: partial\\n\\nevent: error\\ndata: failed\\n\\n',
  ];
  let index = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          async read() {
            if (index < chunks.length) {
              return {value: new TextEncoder().encode(chunks[index++]), done: false};
            }
            return {value: undefined, done: true};
          },
        };
      },
    },
  };
};
const {streamInto} = require(process.argv[1]);
const appended = [];
const target = {
  hidden: true,
  textContent: 'partial',
  dataset: {},
  style: {},
  append(node) { appended.push({className: node.className, text: node.textContent}); },
};
let errorCount = 0;
streamInto('/x', null, target, null, () => { errorCount += 1; }).then(() => {
  process.stdout.write(JSON.stringify({
    state: target.dataset.streamState,
    streaming: target.dataset.streaming,
    errorCount,
    textContent: target.textContent,
    appended,
  }));
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_stream_into_surfaces_failed_state_and_clears_busy_flag():
    out = subprocess.run(
        ["node", "-e", _STREAM_ERROR_HARNESS, str(APP_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(out) == {
        "state": "failed",
        "errorCount": 1,
        "textContent": "partial",
        "appended": [
            {"className": "notice notice--failed", "text": "Generation interrupted"}
        ],
    }


# Regression tests for item 12: a 413 context_too_large preflight failure used to be indistinguishable
# from any other stream failure -- streamInto() discarded the response body entirely and rendered
# only the generic "Generation interrupted" notice. The owner must see the estimated request size,
# the model's limit, and actionable next steps (plan lines 174-175), while every OTHER failure
# (network errors, 500s, mid-stream `error` events, non-JSON bodies) must keep the exact prior
# generic behavior.
_CONTEXT_TOO_LARGE_HARNESS = """
globalThis.document = {
  body: {addEventListener() {}},
  cookie: '',
  getElementById() {
    return {textContent: JSON.stringify({
      streamInterrupted: 'Generation interrupted',
      contextTooLarge: 'Too large: about {estimated} tokens, limit is {limit}.',
      contextTooLargeActions: 'Pick a bigger model or trim history.',
    })};
  },
  createElement() {
    return {className: '', textContent: ''};
  },
};
globalThis.fetch = async () => ({
  ok: false,
  status: 413,
  body: null,
  async json() {
    return {
      detail: {
        reason: 'context_too_large',
        estimated_tokens: 5000,
        limit: 4000,
        model: 'test-model',
        target_kind: 'entry_commentary',
        target_id: null,
      },
    };
  },
});
const {streamInto} = require(process.argv[1]);
const appended = [];
const target = {
  hidden: true,
  textContent: '',
  dataset: {},
  style: {},
  append(...nodes) {
    for (const node of nodes) appended.push({className: node.className, text: node.textContent});
  },
};
let capturedError = null;
streamInto('/x', null, target, null, (error) => { capturedError = {status: error.status, detail: error.detail}; }).then(() => {
  process.stdout.write(JSON.stringify({
    state: target.dataset.streamState,
    streaming: target.dataset.streaming,
    appended,
    capturedError,
  }));
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_stream_into_renders_estimated_size_and_limit_and_actions_on_context_too_large():
    out = subprocess.run(
        ["node", "-e", _CONTEXT_TOO_LARGE_HARNESS, str(APP_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = json.loads(out)

    assert result["state"] == "failed"
    assert "streaming" not in result  # re-entrancy flag cleared on every failure path too
    assert result["appended"] == [
        {"className": "notice notice--failed", "text": "Too large: about 5000 tokens, limit is 4000."},
        {"className": "notice notice--failed-actions", "text": "Pick a bigger model or trim history."},
    ]
    # onError receives the stable detail so callers (chat.js/entry.js/etc) could inspect it too.
    assert result["capturedError"] == {
        "status": 413,
        "detail": {
            "reason": "context_too_large",
            "estimated_tokens": 5000,
            "limit": 4000,
            "model": "test-model",
            "target_kind": "entry_commentary",
            "target_id": None,
        },
    }


# A DIFFERENT stable reason (e.g. maintenance_locked) or an unparseable body must NOT trigger the
# context_too_large rendering -- they fall through to the exact same generic notice as before this
# change, proving the new branch is additive and doesn't widen its scope by accident.
_NON_CONTEXT_TOO_LARGE_HARNESS = """
globalThis.document = {
  body: {addEventListener() {}},
  cookie: '',
  getElementById() {
    return {textContent: JSON.stringify({
      streamInterrupted: 'Generation interrupted',
      contextTooLarge: 'Too large: about {estimated} tokens, limit is {limit}.',
      contextTooLargeActions: 'Pick a bigger model or trim history.',
    })};
  },
  createElement() {
    return {className: '', textContent: ''};
  },
};
globalThis.fetch = async () => ({
  ok: false,
  status: 503,
  body: null,
  async json() {
    return {detail: {reason: 'maintenance_locked'}};
  },
});
const {streamInto} = require(process.argv[1]);
const appended = [];
const target = {
  hidden: true,
  textContent: '',
  dataset: {},
  style: {},
  append(...nodes) {
    for (const node of nodes) appended.push({className: node.className, text: node.textContent});
  },
};
streamInto('/x', null, target).then(() => {
  process.stdout.write(JSON.stringify({state: target.dataset.streamState, appended}));
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_stream_into_keeps_generic_notice_for_other_stable_reasons():
    out = subprocess.run(
        ["node", "-e", _NON_CONTEXT_TOO_LARGE_HARNESS, str(APP_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = json.loads(out)

    assert result["state"] == "failed"
    assert result["appended"] == [{"className": "notice notice--failed", "text": "Generation interrupted"}]


# A 500 with a non-JSON (or empty) body must also fall through to the generic notice, never throw
# out of parseStableErrorDetail's own try/catch.
_NON_JSON_BODY_HARNESS = """
globalThis.document = {
  body: {addEventListener() {}},
  cookie: '',
  getElementById() {
    return {textContent: JSON.stringify({streamInterrupted: 'Generation interrupted'})};
  },
  createElement() {
    return {className: '', textContent: ''};
  },
};
globalThis.fetch = async () => ({
  ok: false,
  status: 500,
  body: null,
  async json() { throw new Error('not json'); },
});
const {streamInto} = require(process.argv[1]);
const appended = [];
const target = {
  hidden: true,
  textContent: '',
  dataset: {},
  style: {},
  append(...nodes) {
    for (const node of nodes) appended.push({className: node.className, text: node.textContent});
  },
};
streamInto('/x', null, target).then(() => {
  process.stdout.write(JSON.stringify({state: target.dataset.streamState, appended}));
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_stream_into_handles_non_json_error_body_generically():
    out = subprocess.run(
        ["node", "-e", _NON_JSON_BODY_HARNESS, str(APP_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = json.loads(out)

    assert result["state"] == "failed"
    assert result["appended"] == [{"className": "notice notice--failed", "text": "Generation interrupted"}]


_COMPOSER_HARNESS = """
globalThis.document = {body: {addEventListener() {}}, cookie: ''};
const {bindComposer} = require(process.argv[1]);
const inputHandlers = {};
const formHandlers = {};
const input = {
  value: '  hello  ',
  scrollHeight: 240,
  style: {},
  addEventListener(type, callback) { inputHandlers[type] = callback; },
};
const submit = {disabled: false};
let requestSubmits = 0;
const form = {
  dataset: {},
  querySelector(selector) { return selector === 'textarea' ? input : submit; },
  addEventListener(type, callback) { formHandlers[type] = callback; },
  requestSubmit() { requestSubmits += 1; },
};
let release;
const pending = new Promise((resolve) => { release = resolve; });
let calls = 0;
let submittedMessage = null;
bindComposer(form, async (message) => {
  calls += 1;
  submittedMessage = message;
  await pending;
});

(async () => {
  inputHandlers.input();
  let prevented = 0;
  inputHandlers.keydown({
    key: 'Enter', shiftKey: false, isComposing: false,
    preventDefault() { prevented += 1; },
  });
  inputHandlers.keydown({
    key: 'Enter', shiftKey: true, isComposing: false,
    preventDefault() { prevented += 1; },
  });
  const first = formHandlers.submit({preventDefault() {}});
  const second = formHandlers.submit({preventDefault() {}});
  await Promise.resolve();
  const during = {busy: form.dataset.busy, disabled: submit.disabled, calls};
  release();
  await Promise.all([first, second]);
  process.stdout.write(JSON.stringify({
    height: input.style.height,
    requestSubmits,
    prevented,
    submittedMessage,
    during,
    after: {busy: form.dataset.busy, disabled: submit.disabled, calls},
  }));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_bind_composer_grows_submits_and_blocks_duplicate_turns():
    out = subprocess.run(
        ["node", "-e", _COMPOSER_HARNESS, str(APP_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(out) == {
        "height": "192px",
        "requestSubmits": 1,
        "prevented": 1,
        "submittedMessage": "hello",
        "during": {"busy": "1", "disabled": True, "calls": 1},
        "after": {"disabled": False, "calls": 1},
    }
