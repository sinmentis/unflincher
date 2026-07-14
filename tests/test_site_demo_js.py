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
        "  unknown: normalizeView('nope'),"
        "  empty: normalizeView('')"
        "}));"
    )
    assert json.loads(output) == {
        "report": "report",
        "upper": "workshop",
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
        "const data = {entries:[{id:'e1',date:'2021-01-01',title:'<script>x</script>',body:'b',commentary:'c'}]};"
        "process.stdout.write(renderView('timeline', data, null));"
    )
    assert "Timeline" in output
    assert "&lt;script&gt;x&lt;/script&gt;" in output
    assert "<script>x</script>" not in output


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_conversation_view_explains_that_live_chat_is_disabled():
    output = _run_node(
        "const {renderView} = require(process.argv[1]);"
        "const data = {conversation:{title:'Conversation',messages:[{role:'user',text:'Hello'}]}};"
        "process.stdout.write(renderView('conversation', data, null));"
    )
    assert "Continue conversation" in output
    assert "Available in the self-hosted app." in output


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
const fixture = JSON.stringify({meta:{synthetic:true},entries:[{id:'e1',date:'2021-01-01',title:'First',body:'b',commentary:'c'}]});
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
const root = {
  getAttribute: () => "timeline",
  querySelector: (sel) => (sel === "[data-demo-stage]" ? stage : null),
  querySelectorAll: () => [],
};
const fakeFetch = async () => ({ok: false, status: 404, text: async () => ""});
initDemo(root, fakeFetch, "data.json").then(() => {
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


def test_demo_page_states_sample_data_and_five_views():
    html = (ROOT / "site" / "demo" / "index.html").read_text(encoding="utf-8")
    assert 'lang="en"' in html
    assert "Sample data" in html
    assert "<h2>Explore five views</h2>" in html
    assert "GitHub Pages" in html
    assert "platform logging and privacy practices" in html
    assert "data-static-fallback" in html
    assert "<noscript>" not in html
    for label in ("Timeline", "Entry and commentary", "Life Report", "Conversation", "Prompt Workshop"):
        assert label in html
    assert html.count('data-view="') == 5
    for image in (
        "demo-timeline.png",
        "demo-entry.png",
        "demo-report.png",
        "demo-conversation.png",
        "demo-workshop.png",
    ):
        assert image in html
    assert "local SQLite database" in html
    assert "GitHub Copilot" in html
    assert 'href="/' not in html
