import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_JS = Path(__file__).resolve().parents[1] / "src" / "unflincher" / "static" / "js"


def _run_node(module_name: str, source: str) -> str:
    return subprocess.run(
        ["node", "-e", source, str(STATIC_JS / module_name)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_entry_module_loads_without_browser_globals():
    output = _run_node(
        "entry.js",
        "const {initEntryPage} = require(process.argv[1]); process.stdout.write(typeof initEntryPage);",
    )
    assert output == "function"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_chat_session_title_validation_rejects_blank_values():
    output = _run_node(
        "chat.js",
        """
        const {isValidSessionTitle} = require(process.argv[1]);
        process.stdout.write(JSON.stringify({
          blank: isValidSessionTitle('   '),
          title: isValidSessionTitle('Choosing without certainty'),
        }));
        """,
    )
    assert json.loads(output) == {"blank": False, "title": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_timeline_module_loads_without_browser_globals():
    output = _run_node(
        "timeline.js",
        "const {initTimeline} = require(process.argv[1]); process.stdout.write(typeof initTimeline);",
    )
    assert output == "function"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_report_heading_descriptions_preserve_ids_and_generate_stable_fallbacks():
    output = _run_node(
        "report.js",
        """
        const {REPORT_HEADING_SELECTOR, describeReportHeadings} = require(process.argv[1]);
        const result = describeReportHeadings([
          {id: 'existing', textContent: 'Pattern'},
          {id: '', textContent: '  Cost of delay  '},
        ]);
        process.stdout.write(JSON.stringify({selector: REPORT_HEADING_SELECTOR, result}));
        """,
    )
    assert json.loads(output) == {
        "selector": "h2, h3, h4",
        "result": [
            {"id": "existing", "label": "Pattern"},
            {"id": "report-section-2", "label": "Cost of delay"},
        ],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_new_entry_local_date_string_does_not_convert_to_utc():
    output = _run_node(
        "new-entry.js",
        """
        const {localDateString} = require(process.argv[1]);
        process.stdout.write(localDateString(new Date(2026, 6, 13, 23, 30, 0)));
        """,
    )
    assert output == "2026-07-13"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_apply_and_regenerate_uses_one_atomic_request():
    output = _run_node(
        "workshop.js",
        """
        const {applyAndRegenerate} = require(process.argv[1]);
        const calls = [];
        async function fakeFetch(url, options) {
          calls.push({
            url,
            method: options.method,
            contentType: options.headers["Content-Type"],
            csrf: options.headers["X-CSRF-Token"],
            body: JSON.parse(options.body),
          });
          return {ok: true, status: 200, json: async () => ({job_id: 42, preset_key: 'analyst'})};
        }
        applyAndRegenerate(fakeFetch, {draft_prompt: 'p', model: 'm'}, 'csrf').then((result) => {
          process.stdout.write(JSON.stringify({...result, calls}));
        });
        """,
    )
    assert json.loads(output) == {
        "jobId": 42,
        "presetKey": "analyst",
        "calls": [
            {
                "url": "/workshop/apply-all",
                "method": "POST",
                "contentType": "application/json",
                "csrf": "csrf",
                "body": {"draft_prompt": "p", "model": "m"},
            },
        ],
    }


APP_JS = Path(__file__).resolve().parents[1] / "src" / "unflincher" / "static" / "app.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_apply_and_regenerate_carries_stable_error_detail_on_413():
    """Regression test for item 5: apply-all's error must carry the same estimated_tokens/limit
    detail streamInto() surfaces elsewhere, not just an HTTP status, so the click handler can
    render the same localized capacity message + actions."""
    output = subprocess.run(
        [
            "node", "-e",
            """
            globalThis.document = {addEventListener() {}, body: {addEventListener() {}}, cookie: ''};
            const {parseStableErrorDetail} = require(process.argv[1]);
            globalThis.parseStableErrorDetail = parseStableErrorDetail;
            const {applyAndRegenerate} = require(process.argv[2]);

            async function fakeFetch() {
              return {
                ok: false,
                status: 413,
                async json() {
                  return {
                    detail: {
                      reason: 'context_too_large', estimated_tokens: 5000, limit: 4000,
                      model: 'test-model', target_kind: 'aggregate_report', target_id: null,
                    },
                  };
                },
              };
            }

            applyAndRegenerate(fakeFetch, {draft_prompt: 'p', model: 'm'}, 'csrf')
              .then(() => { process.stdout.write(JSON.stringify({threw: false})); })
              .catch((error) => {
                process.stdout.write(JSON.stringify({
                  threw: true, status: error.status, detail: error.detail,
                }));
              });
            """,
            str(APP_JS), str(STATIC_JS / "workshop.js"),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(output)

    assert result["threw"] is True
    assert result["status"] == 413
    assert result["detail"] == {
        "reason": "context_too_large", "estimated_tokens": 5000, "limit": 4000,
        "model": "test-model", "target_kind": "aggregate_report", "target_id": None,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_apply_and_regenerate_error_detail_is_null_on_unparseable_body():
    """A 500 with a non-JSON body must not throw out of applyAndRegenerate itself -- detail is
    simply null, and the caller falls back to its existing generic failure notice."""
    output = subprocess.run(
        [
            "node", "-e",
            """
            globalThis.document = {addEventListener() {}, body: {addEventListener() {}}, cookie: ''};
            const {parseStableErrorDetail} = require(process.argv[1]);
            globalThis.parseStableErrorDetail = parseStableErrorDetail;
            const {applyAndRegenerate} = require(process.argv[2]);

            async function fakeFetch() {
              return {ok: false, status: 500, async json() { throw new Error('not json'); }};
            }

            applyAndRegenerate(fakeFetch, {draft_prompt: 'p', model: 'm'}, 'csrf')
              .then(() => { process.stdout.write(JSON.stringify({threw: false})); })
              .catch((error) => {
                process.stdout.write(JSON.stringify({
                  threw: true, status: error.status, detail: error.detail,
                }));
              });
            """,
            str(APP_JS), str(STATIC_JS / "workshop.js"),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(output)

    assert result == {"threw": True, "status": 500, "detail": None}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_stable_error_notice_message_combines_estimate_limit_and_actions():
    """Regression test for the shared entry.js/workshop.js/htmx-retry renderer (app.js's
    stableErrorNoticeMessage): for context_too_large it must combine the localized capacity
    message AND the actions line into one string (these are plain-text `.notice` divs, unlike
    streamInto's own two-paragraph rendering) -- any other reason (or no detail at all) must
    fall through unchanged to the caller's fallback message."""
    output = subprocess.run(
        [
            "node", "-e",
            """
            globalThis.document = {
              body: {addEventListener() {}},
              cookie: '',
              getElementById() {
                return {textContent: JSON.stringify({
                  contextTooLarge: 'Too large: {estimated} > {limit}.',
                  contextTooLargeActions: 'Pick a bigger model.',
                })};
              },
            };
            const {stableErrorNoticeMessage} = require(process.argv[1]);
            const detail = {reason: 'context_too_large', estimated_tokens: 5000, limit: 4000};
            const other = {reason: 'maintenance_locked'};
            process.stdout.write(JSON.stringify({
              contextTooLarge: stableErrorNoticeMessage(detail, 'fallback'),
              otherReason: stableErrorNoticeMessage(other, 'fallback'),
              noDetail: stableErrorNoticeMessage(null, 'fallback'),
            }));
            """,
            str(APP_JS),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(output)

    assert result["contextTooLarge"] == "Too large: 5000 > 4000. Pick a bigger model."
    assert result["otherReason"] == "fallback"
    assert result["noDetail"] == "fallback"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_parse_stable_error_detail_from_text_matches_the_fetch_based_parser():
    """The htmx-retry path reads a synchronous XHR responseText string rather than awaiting a
    fetch Response's .json() -- both must agree on what counts as a stable detail."""
    output = subprocess.run(
        [
            "node", "-e",
            """
            globalThis.document = {body: {addEventListener() {}}, cookie: ''};
            const {parseStableErrorDetailFromText} = require(process.argv[1]);
            process.stdout.write(JSON.stringify({
              stable: parseStableErrorDetailFromText(JSON.stringify({
                detail: {reason: 'context_too_large', estimated_tokens: 1, limit: 2},
              })),
              malformed: parseStableErrorDetailFromText('not json'),
              wrongShape: parseStableErrorDetailFromText(JSON.stringify({detail: 'plain string'})),
            }));
            """,
            str(APP_JS),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(output)

    assert result["stable"] == {"reason": "context_too_large", "estimated_tokens": 1, "limit": 2}
    assert result["malformed"] is None
    assert result["wrongShape"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_htmx_response_error_handler_only_fires_for_generation_retry_elements():
    """The htmx:responseError listener in app.js must be scoped to
    `[data-generation-retry]` -- proving it does not fire for this app's OTHER htmx requests
    (job-progress / commentary-status polling), and that it renders into #workshop-notice when
    it does fire."""
    output = subprocess.run(
        [
            "node", "-e",
            """
            const handlers = {};
            const noticeEl = {textContent: '', dataset: {}, hidden: true};
            globalThis.document = {
              body: {
                addEventListener(type, cb) { handlers[type] = cb; },
              },
              cookie: '',
              getElementById(id) {
                if (id === 'ui-messages') {
                  return {textContent: JSON.stringify({
                    requestFailed: 'Request failed',
                    contextTooLarge: 'Too large: {estimated} > {limit}.',
                    contextTooLargeActions: 'Pick a bigger model.',
                  })};
                }
                if (id === 'workshop-notice') return noticeEl;
                return null;
              },
            };
            require(process.argv[1]);  // app.js -- registers the htmx:responseError handler

            const retryElt = {matches: (sel) => sel === '[data-generation-retry]'};
            const otherElt = {matches: () => false};

            // Case 1: an UNRELATED htmx request (e.g. job-progress polling) must be ignored.
            handlers['htmx:responseError']({
              detail: {elt: otherElt, xhr: {responseText: JSON.stringify({
                detail: {reason: 'context_too_large', estimated_tokens: 5000, limit: 4000},
              })}},
            });
            const afterUnrelated = {text: noticeEl.textContent, hidden: noticeEl.hidden};

            // Case 2: the retry button's own failure IS handled.
            handlers['htmx:responseError']({
              detail: {elt: retryElt, xhr: {responseText: JSON.stringify({
                detail: {reason: 'context_too_large', estimated_tokens: 5000, limit: 4000},
              })}},
            });
            const afterRetry = {text: noticeEl.textContent, tone: noticeEl.dataset.tone, hidden: noticeEl.hidden};

            process.stdout.write(JSON.stringify({afterUnrelated, afterRetry}));
            """,
            str(APP_JS),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(output)

    # The unrelated element's failure never touched the notice at all.
    assert result["afterUnrelated"] == {"text": "", "hidden": True}
    # The retry button's failure rendered the combined capacity message + actions.
    assert result["afterRetry"] == {
        "text": "Too large: 5000 > 4000. Pick a bigger model.",
        "tone": "failed",
        "hidden": False,
    }


_ENTRY_TRIGGER_HARNESS_PRELUDE = """
globalThis.window = {};
globalThis.document = {
  addEventListener() {},
  body: {addEventListener() {}},
  cookie: '',
  getElementById(id) {
    if (id === 'ui-messages') {
      return {textContent: JSON.stringify({
        busy: 'Busy',
        requestFailed: 'Request failed',
        contextTooLarge: 'Too large: {estimated} > {limit}.',
        contextTooLargeActions: 'Pick a bigger model.',
      })};
    }
    return null;
  },
};
const {setNotice, clearNotice, parseStableErrorDetail, stableErrorNoticeMessage} = require(process.argv[1]);
globalThis.setNotice = setNotice;
globalThis.clearNotice = clearNotice;
globalThis.parseStableErrorDetail = parseStableErrorDetail;
globalThis.stableErrorNoticeMessage = stableErrorNoticeMessage;
globalThis.getCsrfToken = () => 'fake-csrf';

const {initEntryPage} = require(process.argv[2]);

const noticeEl = {textContent: '', dataset: {}, hidden: true};
let clickHandler;
const triggerEl = {
  dataset: {endpoint: '/entry/1/commentary'},
  disabled: false,
  addEventListener(type, cb) { if (type === 'click') clickHandler = cb; },
};
const fakeDoc = {
  getElementById(id) {
    if (id === 'commentary-notice') return noticeEl;
    if (id === 'run-commentary') return triggerEl;
    return null;
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
initEntryPage(fakeDoc);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_entry_commentary_trigger_shows_estimate_limit_and_actions_on_413():
    """Regression test for item 5: the single-entry commentary trigger (a plain fetch, not
    streamInto) must show the same localized capacity message + actions on a stable 413
    context_too_large, not just a generic failure notice."""
    output = subprocess.run(
        [
            "node", "-e",
            _ENTRY_TRIGGER_HARNESS_PRELUDE + """
            globalThis.fetch = async () => ({
              ok: false,
              status: 413,
              async json() {
                return {detail: {reason: 'context_too_large', estimated_tokens: 5000, limit: 4000}};
              },
            });

            clickHandler().then(() => {
              process.stdout.write(JSON.stringify({
                text: noticeEl.textContent, tone: noticeEl.dataset.tone, hidden: noticeEl.hidden,
                disabled: triggerEl.disabled,
              }));
            });
            """,
            str(APP_JS), str(STATIC_JS / "entry.js"),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(output)

    assert result == {
        "text": "Too large: 5000 > 4000. Pick a bigger model.",
        "tone": "failed",
        "hidden": False,
        "disabled": False,  # re-enabled in `finally`, matching every other outcome
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_entry_commentary_trigger_keeps_generic_notice_for_other_failures():
    """A 500 with an unparseable body (or any non-context_too_large stable reason) must keep the
    exact prior generic failure text -- proving the new branch is additive, not a regression for
    every other failure mode."""
    output = subprocess.run(
        [
            "node", "-e",
            _ENTRY_TRIGGER_HARNESS_PRELUDE + """
            globalThis.fetch = async () => ({
              ok: false,
              status: 500,
              async json() { throw new Error('not json'); },
            });

            clickHandler().then(() => {
              process.stdout.write(JSON.stringify({
                text: noticeEl.textContent, tone: noticeEl.dataset.tone, hidden: noticeEl.hidden,
              }));
            });
            """,
            str(APP_JS), str(STATIC_JS / "entry.js"),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(output)

    assert result == {"text": "Request failed", "tone": "failed", "hidden": False}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_entry_commentary_trigger_still_reports_busy_on_409():
    """The pre-existing 409 busy-alert path must be completely unaffected by this change."""
    output = subprocess.run(
        [
            "node", "-e",
            _ENTRY_TRIGGER_HARNESS_PRELUDE + """
            globalThis.fetch = async () => ({ok: false, status: 409});

            clickHandler().then(() => {
              process.stdout.write(JSON.stringify({
                text: noticeEl.textContent, tone: noticeEl.dataset.tone, hidden: noticeEl.hidden,
              }));
            });
            """,
            str(APP_JS), str(STATIC_JS / "entry.js"),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(output)

    assert result == {"text": "Busy", "tone": "busy", "hidden": False}
