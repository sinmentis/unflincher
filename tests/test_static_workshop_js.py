"""JS/DOM contract tests for the Prompt Workshop's Perspective picker (workstream 5): preset-fill
on selection, edit-to-Custom switching, the payload sent to apply/apply-all, active-perspective
label updates, and the extended stableErrorNoticeMessage reasons. Uses the same Node-subprocess
harness pattern as tests/test_static_page_js.py (real shipped JS files, plain-object DOM stubs)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_JS = Path(__file__).resolve().parents[1] / "src" / "unflincher" / "static" / "js"
APP_JS = Path(__file__).resolve().parents[1] / "src" / "unflincher" / "static" / "app.js"

_HARNESS_PRELUDE = """
globalThis.window = {};

function withHandlers(obj) {
  obj._handlers = {};
  obj.addEventListener = (type, cb) => { obj._handlers[type] = cb; };
  return obj;
}
function fire(obj, type) { return obj._handlers[type](); }

const radios = [
  withHandlers({id: 'perspective-companion', value: 'companion', checked: false}),
  withHandlers({id: 'perspective-coach', value: 'coach', checked: false}),
  withHandlers({id: 'perspective-challenger', value: 'challenger', checked: false}),
  withHandlers({id: 'perspective-analyst', value: 'analyst', checked: true}),
  withHandlers({id: 'perspective-custom', value: 'custom', checked: false}),
];
function selectRadio(value) {
  radios.forEach((r) => { r.checked = (r.value === value); });
  fire(radios.find((r) => r.value === value), 'change');
}

const textarea = withHandlers({value: 'ANALYST TEXT'});
function typeInto(text) {
  textarea.value = text;
  fire(textarea, 'input');
}

const perspectiveDataNode = {textContent: JSON.stringify({
  companion: {prompt: 'COMPANION TEXT', name: 'Companion'},
  coach: {prompt: 'COACH TEXT', name: 'Coach'},
  challenger: {prompt: 'CHALLENGER TEXT', name: 'Challenger'},
  analyst: {prompt: 'ANALYST TEXT', name: 'Analyst'},
  custom: {prompt: null, name: 'Custom'},
})};

const activeLabel = {textContent: ''};
const modelNotice = {textContent: '', dataset: {}, hidden: true};
const workshopNotice = {textContent: '', dataset: {}, hidden: true};
const confirmBtn = withHandlers({disabled: false, focus() {}});
const cancelBtn = withHandlers({disabled: false, focus() {}});
const applyAllConfirmation = {
  hidden: true,
  querySelector(sel) {
    if (sel === '[data-confirm]') return confirmBtn;
    if (sel === '[data-cancel]') return cancelBtn;
    return null;
  },
};

const elements = {
  'prompt-draft': textarea,
  'perspective-data': perspectiveDataNode,
  'model-select': {value: 'test-model'},
  'refresh-models': withHandlers({disabled: false}),
  'model-notice': modelNotice,
  'run-test': withHandlers({disabled: false}),
  'test-entry': {value: '1'},
  'preview-stream': {},
  'apply-btn': withHandlers({disabled: false, dataset: {savedLabel: 'Saved'}}),
  'workshop-notice': workshopNotice,
  'apply-all-confirmation': applyAllConfirmation,
  'apply-all-btn': withHandlers({disabled: false, focus() {}}),
  'loading-state-template': {content: {cloneNode() { return {}; }}},
  'regen-progress-holder': {replaceChildren() {}},
  'lang-select': withHandlers({value: 'en', disabled: false}),
  'ui-messages': {textContent: JSON.stringify({
    working: 'Working',
    busy: 'Busy',
    requestFailed: 'Request failed',
    contextTooLarge: 'Too large: {estimated} > {limit}.',
    contextTooLargeActions: 'Pick a bigger model.',
    unsupportedModel: 'Unsupported model.',
    modelCatalogOutage: 'Catalog outage.',
    emptyInstructions: 'Instructions empty.',
    activePerspectiveLabel: 'Active Perspective: {name}',
  })},
};

globalThis.document = {
  addEventListener() {},
  body: {addEventListener() {}},
  cookie: '',
  getElementById(id) { return elements[id] || null; },
  querySelector(sel) {
    if (sel === '[data-role="active-perspective"]') return activeLabel;
    return null;
  },
  querySelectorAll(sel) {
    if (sel === 'input[name="perspective-choice"]') return radios;
    return [];
  },
  createElement() { return withHandlers({setAttribute() {}}); },
};
globalThis.getCsrfToken = () => 'fake-csrf';
globalThis.htmx = {process() {}};

const {
  setNotice, clearNotice, parseStableErrorDetail, stableErrorNoticeMessage, streamInto,
} = require(process.argv[1]);
globalThis.setNotice = setNotice;
globalThis.clearNotice = clearNotice;
globalThis.parseStableErrorDetail = parseStableErrorDetail;
globalThis.stableErrorNoticeMessage = stableErrorNoticeMessage;
globalThis.streamInto = streamInto;

const {initWorkshopPage, readPerspectiveData} = require(process.argv[2]);
initWorkshopPage(globalThis.document);
"""


def _run(tail: str) -> str:
    return subprocess.run(
        ["node", "-e", _HARNESS_PRELUDE + tail, str(APP_JS), str(STATIC_JS / "workshop.js")],
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_read_perspective_data_parses_the_json_blob():
    output = subprocess.run(
        [
            "node", "-e",
            """
            globalThis.document = {
              addEventListener() {},
              getElementById(id) {
                if (id !== 'perspective-data') return null;
                return {textContent: JSON.stringify({analyst: {prompt: 'x', name: 'Analyst'}})};
              },
            };
            const {readPerspectiveData} = require(process.argv[1]);
            process.stdout.write(JSON.stringify(readPerspectiveData(globalThis.document)));
            """,
            str(STATIC_JS / "workshop.js"),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    assert json.loads(output) == {"analyst": {"prompt": "x", "name": "Analyst"}}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_read_perspective_data_returns_empty_object_when_blob_is_missing():
    output = subprocess.run(
        [
            "node", "-e",
            """
            globalThis.document = {addEventListener() {}, getElementById() { return null; }};
            const {readPerspectiveData} = require(process.argv[1]);
            process.stdout.write(JSON.stringify(readPerspectiveData(globalThis.document)));
            """,
            str(STATIC_JS / "workshop.js"),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    assert json.loads(output) == {}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_selecting_a_preset_fills_the_textarea_with_its_exact_server_provided_text():
    output = _run("""
    selectRadio('coach');
    process.stdout.write(JSON.stringify({value: textarea.value}));
    """)
    assert json.loads(output) == {"value": "COACH TEXT"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_editing_the_textarea_after_a_preset_selection_switches_to_custom():
    output = _run("""
    selectRadio('coach');
    typeInto('COACH TEXT plus an edit');
    const custom = radios.find((r) => r.value === 'custom');
    process.stdout.write(JSON.stringify({customChecked: custom.checked}));
    """)
    assert json.loads(output) == {"customChecked": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_editing_the_textarea_without_ever_selecting_a_preset_does_not_touch_radios():
    """The textarea starts with Custom already selected (a legacy/custom active prompt) --
    typing must never spontaneously re-select a preset radio."""
    output = _run("""
    radios.forEach((r) => { r.checked = (r.value === 'custom'); });
    typeInto('totally custom text');
    process.stdout.write(JSON.stringify({
      customChecked: radios.find((r) => r.value === 'custom').checked,
      coachChecked: radios.find((r) => r.value === 'coach').checked,
    }));
    """)
    assert json.loads(output) == {"customChecked": True, "coachChecked": False}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_selecting_custom_directly_never_overwrites_the_textarea():
    output = _run("""
    const before = textarea.value;
    selectRadio('custom');
    process.stdout.write(JSON.stringify({unchanged: textarea.value === before}));
    """)
    assert json.loads(output) == {"unchanged": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_apply_click_sends_the_currently_selected_preset_key():
    output = _run("""
    globalThis.fetch = async (url, options) => {
      globalThis.__lastApplyBody = JSON.parse(options.body);
      return {ok: true, status: 200, async json() { return {preset_key: 'coach'}; }};
    };
    selectRadio('coach');
    fire(elements['apply-btn'], 'click');
    setTimeout(() => {
      process.stdout.write(JSON.stringify({
        body: globalThis.__lastApplyBody, activeLabel: activeLabel.textContent,
      }));
    }, 10);
    """)
    result = json.loads(output)
    assert result["body"] == {"draft_prompt": "COACH TEXT", "model": "test-model", "preset_key": "coach"}
    assert result["activeLabel"] == "Active Perspective: Coach"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_apply_click_sends_null_preset_key_after_switching_to_custom():
    output = _run("""
    globalThis.fetch = async (url, options) => {
      globalThis.__lastApplyBody = JSON.parse(options.body);
      return {ok: true, status: 200, async json() { return {preset_key: null}; }};
    };
    selectRadio('coach');
    typeInto('an edited version of the coach preset');
    fire(elements['apply-btn'], 'click');
    setTimeout(() => {
      process.stdout.write(JSON.stringify({body: globalThis.__lastApplyBody}));
    }, 10);
    """)
    result = json.loads(output)
    assert result["body"]["preset_key"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_apply_all_click_sends_the_currently_selected_preset_key():
    output = _run("""
    globalThis.fetch = async (url, options) => {
      globalThis.__lastApplyAllBody = JSON.parse(options.body);
      return {ok: true, status: 200, async json() { return {job_id: 7, preset_key: 'analyst'}; }};
    };
    selectRadio('analyst');
    fire(elements['apply-all-btn'], 'click');
    fire(confirmBtn, 'click');
    setTimeout(() => {
      process.stdout.write(JSON.stringify({
        body: globalThis.__lastApplyAllBody, activeLabel: activeLabel.textContent,
      }));
    }, 10);
    """)
    result = json.loads(output)
    assert result["body"] == {"draft_prompt": "ANALYST TEXT", "model": "test-model", "preset_key": "analyst"}
    assert result["activeLabel"] == "Active Perspective: Analyst"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_apply_all_active_label_reflects_the_server_response_not_the_sent_intent():
    """Regression: the sent intent is null (Custom) -- but the server resolved it to 'analyst'
    (e.g. the Custom-labeled text happened to exactly match the shipped Analyst preset). The
    active-perspective label must reflect the SERVER's classification, never the browser's
    stale/forged/mismatched intent."""
    output = _run("""
    globalThis.fetch = async (url, options) => {
      globalThis.__lastApplyAllBody = JSON.parse(options.body);
      return {ok: true, status: 200, async json() { return {job_id: 9, preset_key: 'analyst'}; }};
    };
    selectRadio('custom');
    fire(elements['apply-all-btn'], 'click');
    fire(confirmBtn, 'click');
    setTimeout(() => {
      process.stdout.write(JSON.stringify({
        body: globalThis.__lastApplyAllBody, activeLabel: activeLabel.textContent,
      }));
    }, 10);
    """)
    result = json.loads(output)
    assert result["body"]["preset_key"] is None
    assert result["activeLabel"] == "Active Perspective: Analyst"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_apply_click_renders_localized_message_and_re_enables_button_on_unsupported_model():
    output = _run("""
    globalThis.fetch = async () => ({
      ok: false, status: 400,
      async json() { return {detail: {reason: 'unsupported_model', model: 'x'}}; },
    });
    fire(elements['apply-btn'], 'click');
    setTimeout(() => {
      process.stdout.write(JSON.stringify({
        text: workshopNotice.textContent, tone: workshopNotice.dataset.tone,
        disabled: elements['apply-btn'].disabled,
      }));
    }, 10);
    """)
    assert json.loads(output) == {"text": "Unsupported model.", "tone": "failed", "disabled": False}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_apply_click_renders_localized_message_on_empty_instructions():
    output = _run("""
    globalThis.fetch = async () => ({
      ok: false, status: 400,
      async json() { return {detail: {reason: 'empty_instructions'}}; },
    });
    fire(elements['apply-btn'], 'click');
    setTimeout(() => {
      process.stdout.write(JSON.stringify({text: workshopNotice.textContent}));
    }, 10);
    """)
    assert json.loads(output) == {"text": "Instructions empty."}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_apply_all_click_renders_localized_message_on_model_catalog_outage():
    output = _run("""
    globalThis.fetch = async () => ({
      ok: false, status: 503,
      async json() { return {detail: {reason: 'model_limits_unavailable', model: 'x'}}; },
    });
    fire(elements['apply-all-btn'], 'click');
    fire(confirmBtn, 'click');
    setTimeout(() => {
      process.stdout.write(JSON.stringify({
        text: workshopNotice.textContent,
        confirmDisabled: confirmBtn.disabled, cancelDisabled: cancelBtn.disabled,
        applyAllDisabled: elements['apply-all-btn'].disabled,
      }));
    }, 10);
    """)
    assert json.loads(output) == {
        "text": "Catalog outage.", "confirmDisabled": False, "cancelDisabled": False,
        "applyAllDisabled": False,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_stable_error_notice_message_covers_the_new_workshop_reasons():
    output = subprocess.run(
        [
            "node", "-e",
            """
            globalThis.document = {
              body: {addEventListener() {}},
              cookie: '',
              getElementById() {
                return {textContent: JSON.stringify({
                  unsupportedModel: 'Unsupported model.',
                  modelCatalogOutage: 'Catalog outage.',
                  emptyInstructions: 'Instructions empty.',
                })};
              },
            };
            const {stableErrorNoticeMessage} = require(process.argv[1]);
            process.stdout.write(JSON.stringify({
              unsupported: stableErrorNoticeMessage({reason: 'unsupported_model'}, 'fallback'),
              outage: stableErrorNoticeMessage({reason: 'model_limits_unavailable'}, 'fallback'),
              empty: stableErrorNoticeMessage({reason: 'empty_instructions'}, 'fallback'),
              unknownReason: stableErrorNoticeMessage({reason: 'unknown_preset_key'}, 'fallback'),
            }));
            """,
            str(APP_JS),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(output)
    assert result == {
        "unsupported": "Unsupported model.",
        "outage": "Catalog outage.",
        "empty": "Instructions empty.",
        "unknownReason": "fallback",
    }


def test_workshop_js_never_uses_browser_alert():
    source = (STATIC_JS / "workshop.js").read_text(encoding="utf-8")
    assert "alert(" not in source
