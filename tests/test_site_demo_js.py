import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO_JS = ROOT / "site" / "assets" / "js" / "demo.js"


def _run_node(source: str) -> str:
    return subprocess.run(
        ["node", "-e", source, str(DEMO_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_escape_html_neutralizes_markup():
    output = _run_node(
        "const {escapeHtml} = require(process.argv[1]);"
        "process.stdout.write(escapeHtml(`<b>a&\"'</b>`));"
    )
    assert output == "&lt;b&gt;a&amp;&quot;&#39;&lt;/b&gt;"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_normalize_view_maps_known_and_unknown_views():
    output = _run_node(
        "const {normalizeView} = require(process.argv[1]);"
        "process.stdout.write(JSON.stringify({"
        "  report: normalizeView('report'),"
        "  upper: normalizeView('WORKSHOP'),"
        "  write: normalizeView('WRITE'),"
        "  unknown: normalizeView('nope'),"
        "  empty: normalizeView('')"
        "}));"
    )
    assert json.loads(output) == {
        "report": "report",
        "upper": "workshop",
        "write": "write",
        "unknown": "timeline",
        "empty": "timeline",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_view_from_query_reads_the_view_parameter():
    output = _run_node(
        "const {viewFromQuery} = require(process.argv[1]);"
        "process.stdout.write(JSON.stringify({"
        "  present: viewFromQuery('?view=report'),"
        "  absent: viewFromQuery('?x=1'),"
        "  empty: viewFromQuery('')"
        "}));"
    )
    assert json.loads(output) == {"present": "report", "absent": None, "empty": None}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_parse_fixture_requires_synthetic_flag():
    output = _run_node(
        "const {parseFixture} = require(process.argv[1]);"
        "const bad = parseFixture('{ not json');"
        "const notSynthetic = parseFixture(JSON.stringify({meta:{synthetic:false},entries:[]}));"
        "const good = parseFixture(JSON.stringify({meta:{synthetic:true},entries:[]}));"
        "process.stdout.write(JSON.stringify({bad: bad.ok, badErr: bad.error, ns: notSynthetic.error, good: good.ok}));"
    )
    assert json.loads(output) == {"bad": False, "badErr": "invalid-json", "ns": "not-synthetic", "good": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_render_view_escapes_fixture_strings_and_labels_timeline():
    output = _run_node(
        "const {renderView} = require(process.argv[1]);"
        "const data = {entries:[{id:'e1',date:'2021-01-01',title:'<script>x</script>',body:'b',reflection:'c'}]};"
        "process.stdout.write(renderView('timeline', data, null));"
    )
    assert "Timeline" in output
    assert "&lt;script&gt;x&lt;/script&gt;" in output
    assert "<script>x</script>" not in output


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_entry_view_matches_the_current_segmented_reflection_surface():
    output = _run_node(
        "const {renderView} = require(process.argv[1]);"
        "const data = {entries:[{id:'e1',date:'2022-06-27',title:'One more spreadsheet',body:'b',reflection:'This is the generated reading.'}]};"
        "process.stdout.write(renderView('entry', data, 'e1'));"
    )
    assert "One more spreadsheet" in output
    assert "Wellbeing 78/100" in output
    assert 'data-entry-tab="body"' in output
    assert 'data-entry-tab="reflection"' in output
    assert 'data-entry-tab="conversation"' in output
    assert "This is the generated reading." in output
    assert "Self-hosted app only." in output
    notice_index = output.index('id="demo-locked-entry"')
    button_index = output.index('aria-describedby="demo-locked-entry"')
    assert notice_index < button_index, "the lock notice must appear before the disabled button"
    assert 'aria-describedby="demo-locked-entry"' in output
    assert 'disabled aria-disabled="true"' in output


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_conversation_view_explains_that_live_chat_is_disabled():
    output = _run_node(
        "const {renderView} = require(process.argv[1]);"
        "const data = {conversation:{title:'Conversation',messages:[{role:'user',text:'Hello'}]}};"
        "process.stdout.write(renderView('conversation', data, null));"
    )
    assert "Ask a follow-up question" in output
    assert "Send" in output
    assert "Self-hosted app only." in output


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_write_view_is_present_but_read_only():
    output = _run_node(
        "const {renderView} = require(process.argv[1]);"
        "process.stdout.write(renderView('write', {entries:[]}, null));"
    )
    assert "What do you want to remember?" in output
    assert "Save entry" in output
    assert "Self-hosted app only." in output


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_init_demo_renders_timeline_from_injected_fetch():
    harness = r"""
const {initDemo} = require(process.argv[1]);
function makeStage() {
  return {
    innerHTML: '<div data-static-fallback>Static fallback</div>',
    querySelectorAll: () => [],
  };
}
const stage = makeStage();
const root = {
  getAttribute: () => "timeline",
  querySelector: (sel) => (sel === "[data-demo-stage]" ? stage : null),
  querySelectorAll: () => [],
};
const fixture = JSON.stringify({meta:{synthetic:true},entries:[{id:'e1',date:'2021-01-01',title:'First',body:'b',reflection:'c'}]});
const fakeFetch = async () => ({ok: true, status: 200, text: async () => fixture});
initDemo(root, fakeFetch, "data.json").then(() => {
  process.stdout.write(JSON.stringify({
    hasTitle: stage.innerHTML.includes("First"),
    hasBadge: stage.innerHTML.includes("Timeline"),
    fallbackRemoved: !stage.innerHTML.includes("data-static-fallback"),
  }));
});
"""
    output = _run_node(harness)
    assert json.loads(output) == {
        "hasTitle": True,
        "hasBadge": True,
        "fallbackRemoved": True,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_init_demo_shows_explicit_error_when_fixture_fails():
    harness = r"""
const {initDemo} = require(process.argv[1]);
const stage = {
  innerHTML: '<div data-static-fallback>Static fallback</div>',
  querySelectorAll: () => [],
};
let navHandler = null;
const navButton = {
  getAttribute: () => "report",
  classList: {toggle: () => {}},
  setAttribute: () => {},
  addEventListener: (type, handler) => {
    if (type === "click") navHandler = handler;
  },
};
const root = {
  getAttribute: () => "timeline",
  querySelector: (sel) => (sel === "[data-demo-stage]" ? stage : null),
  querySelectorAll: (sel) => (sel === "[data-view]" ? [navButton] : []),
};
const fakeFetch = async () => ({ok: false, status: 404, text: async () => ""});
initDemo(root, fakeFetch, "data.json").then(() => {
  if (navHandler) navHandler();
  process.stdout.write(JSON.stringify({
    error: stage.innerHTML.includes("could not be loaded"),
    github: stage.innerHTML.includes("github.com/sinmentis/unflincher"),
    fallback: stage.innerHTML.includes("data-static-fallback"),
  }));
});
"""
    output = _run_node(harness)
    assert json.loads(output) == {
        "error": True,
        "github": True,
        "fallback": True,
    }


def test_demo_js_never_persists_state():
    source = DEMO_JS.read_text(encoding="utf-8")
    for forbidden in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert forbidden not in source


def test_demo_page_states_sample_data_and_six_views():
    html = (ROOT / "site" / "demo" / "index.html").read_text(encoding="utf-8")
    assert 'lang="en"' in html
    assert "Sample data" in html
    assert "<h2>Explore six views</h2>" in html
    assert "GitHub Pages" in html
    assert "platform logging and privacy practices" in html
    assert "data-static-fallback" in html
    assert "<noscript>" not in html
    for label in (
        "Timeline",
        "Entry Reflection",
        "Life Report",
        "Conversation",
        "Write",
        "Prompt Workshop",
    ):
        assert label in html
    assert html.count('data-view="') == 6
    for image in (
        "demo-timeline.png",
        "demo-entry.png",
        "demo-report.png",
        "demo-conversation.png",
        "demo-write.png",
        "demo-workshop.png",
    ):
        assert image in html
    assert "local SQLite database" in html
    assert "GitHub Copilot" in html
    assert "active prompt" in html
    assert "no model calls, tracking, cookies, storage, or writable operations" in html
    assert "full Journal Archive" in html
    assert "target and earlier entries" in html
    assert 'href="../#privacy"' in html
    assert "live chat" not in html.lower()
    assert 'href="/' not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_workshop_view_shows_shared_entry_and_all_five_perspectives():
    output = _run_node(
        "const {renderView} = require(process.argv[1]);"
        "const data = {"
        "  entries: [{id: 'e1', date: '2022-06-27', title: 'One more spreadsheet', body: 'b', reflection: 'r'}],"
        "  workshop: {"
        "    entry_id: 'e1',"
        "    perspectives: ["
        "      {key: 'companion', name: 'Companion', instructions: 'i1', reading: 'reads warmly'},"
        "      {key: 'coach', name: 'Coach', instructions: 'i2', reading: 'reads toward action'},"
        "      {key: 'challenger', name: 'Challenger', instructions: 'i3', reading: 'reads directly'},"
        "      {key: 'analyst', name: 'Analyst', instructions: 'i4', reading: 'reads precisely'},"
        "      {key: 'custom', name: 'Custom', instructions: 'i5', reading: 'reads on my own terms'}"
        "    ]"
        "  }"
        "};"
        "process.stdout.write(renderView('workshop', data, null));"
    )
    assert "Prompt Workshop" in output
    assert "2022-06-27" in output
    assert "One more spreadsheet" in output
    for name in ("Companion", "Coach", "Challenger", "Analyst", "Custom"):
        assert name in output
    assert "Apply and regenerate all" in output
    assert "Self-hosted app only." in output
