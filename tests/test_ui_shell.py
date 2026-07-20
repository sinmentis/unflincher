import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import unflincher.llm as llm_module
from unflincher.app import create_app
from unflincher.templates_env import get_ui_state

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "unflincher" / "templates"
SHELL_CSS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "unflincher"
    / "static"
    / "css"
    / "shell.css"
)


@pytest.mark.parametrize(
    ("path", "active_nav", "page_id"),
    [
        ("/", "timeline", "timeline"),
        ("/entry/12", "timeline", "entry"),
        ("/report", "report", "report"),
        ("/report/4", "report", "report"),
        ("/chat", "chat", "chat-list"),
        ("/chat/new", "chat", "chat-session"),
        ("/chat/3", "chat", "chat-session"),
        ("/new", "new_entry", "new-entry"),
        ("/workshop", "workshop", "workshop"),
        ("/missing", None, "error"),
    ],
)
def test_get_ui_state(path, active_nav, page_id):
    assert get_ui_state(path) == (active_nav, page_id)


def test_base_document_has_structured_studio_metadata_and_landmarks(client):
    body = client.get("/").text
    assert '<meta name="theme-color" content="#17191c">' in body
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in body
    assert 'class="skip-link" href="#main-content"' in body
    assert 'class="app-topbar"' in body
    assert 'class="primary-nav"' in body
    assert 'id="main-content"' in body
    assert 'data-page="timeline"' in body
    assert 'data-nav="timeline"' in body
    assert 'aria-current="page"' in body
    assert "UNFLINCHER" in body
    assert "brand-seal" not in body


def test_ui_messages_json_carries_context_too_large_keys_for_streamInto(client):
    """Regression test for item 12: streamInto() (app.js) reads window UI_MESSAGES.contextTooLarge
    and .contextTooLargeActions to render the 413 context_too_large notice. If either key ever
    goes missing from the server-rendered #ui-messages JSON, that notice silently falls back to
    the generic "Generation interrupted" text with no estimate/limit/actions -- catch that here
    at the template layer, independent of the JS-side tests in test_static_app_js.py."""
    import json

    body = client.get("/").text
    start = body.index('id="ui-messages"')
    json_start = body.index(">", start) + 1
    json_end = body.index("</script>", json_start)
    messages = json.loads(body[json_start:json_end])

    assert "{estimated}" in messages["contextTooLarge"]
    assert "{limit}" in messages["contextTooLarge"]
    assert messages["contextTooLargeActions"]
    assert messages["streamInterrupted"]


def test_primary_nav_exposes_each_destination_once(client):
    body = client.get("/chat").text
    for key in ("timeline", "report", "chat", "new_entry", "workshop"):
        assert body.count(f'data-nav="{key}"') == 1
    assert body.count('class="primary-nav"') == 1
    assert 'data-page="chat-list"' in body


def test_mobile_back_link_targets_the_right_hub_on_every_non_timeline_page(client):
    # Below 700px, every page but Timeline swaps the pill nav for one contextual link (CSS
    # handles the swap; both elements always render server-side). Chat sessions point back to
    # the chat list, everything else points to Timeline -- the same two targets the old
    # per-page back link used, just CSS-scoped to mobile now instead of removed outright.
    timeline_body = client.get("/").text
    assert "topbar-back" not in timeline_body
    assert "topbar-mobile-back" not in timeline_body
    assert 'class="primary-nav"' in timeline_body

    for path, expected_href in (
        ("/new", "/"),
        ("/report", "/"),
        ("/workshop", "/"),
        ("/chat", "/"),
        ("/chat/new", "/chat"),
    ):
        body = client.get(path).text
        assert "topbar-back" not in body
        assert f'class="topbar-mobile-back" href="{expected_href}"' in body
        assert 'class="primary-nav"' in body
        for key in ("timeline", "report", "chat", "new_entry", "workshop"):
            assert f'data-nav="{key}"' in body


def test_app_topbar_stacks_brand_above_a_wrapping_pill_nav():
    css = SHELL_CSS.read_text()

    def declarations(selector):
        match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]+)\}}", css)
        assert match is not None
        return match.group("body")

    topbar = declarations(".app-topbar")
    assert "flex-direction: column" in topbar
    nav = declarations(".primary-nav")
    assert "flex-wrap: wrap" in nav


def test_html_404_is_branded_but_json_404_keeps_api_shape(client):
    html = client.get("/does-not-exist", headers={"accept": "text/html"})
    assert html.status_code == 404
    assert "UNFLINCHER" in html.text
    assert 'data-page="error"' in html.text
    assert 'aria-current="page"' not in html.text

    deep_link = client.get("/entry/9999", headers={"accept": "text/html"})
    assert deep_link.status_code == 404
    assert 'data-page="error"' in deep_link.text
    assert 'aria-current="page"' not in deep_link.text

    api = client.get("/entry/9999", headers={"accept": "application/json"})
    assert api.status_code == 404
    assert api.json() == {"detail": "entry not found"}


def test_html_500_is_branded_but_json_500_keeps_api_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("UNFLINCHER_DB", str(tmp_path / "errors.db"))
    monkeypatch.setenv("UNFLINCHER_REQUIRE_ACCESS_AUTH", "false")

    async def _noop():
        pass

    monkeypatch.setattr(llm_module, "warm_up_client", _noop)
    monkeypatch.setattr(llm_module, "shutdown_client", _noop)
    app = create_app()

    @app.get("/workshop/explode")
    async def explode():
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as error_client:
        html = error_client.get("/workshop/explode", headers={"accept": "text/html"})
        assert html.status_code == 500
        assert "UNFLINCHER" in html.text
        assert 'data-page="error"' in html.text
        assert 'aria-current="page"' not in html.text

        api = error_client.get("/workshop/explode", headers={"accept": "application/json"})
        assert api.status_code == 500
        assert api.json() == {"detail": "Internal Server Error"}


def test_favicon_and_ordered_stylesheets_are_served(client):
    favicon = client.get("/static/favicon.svg")
    assert favicon.status_code == 200
    assert "诤" in favicon.text
    body = client.get("/").text
    names = ["tokens.css", "base.css", "shell.css", "components.css", "pages.css"]
    positions = [body.index(f"/static/css/{name}") for name in names]
    assert positions == sorted(positions)


def test_base_is_the_only_template_that_loads_the_shared_browser_script():
    base = (TEMPLATES / "base.html").read_text()
    assert base.count('src="/static/app.js"') == 1
    for path in TEMPLATES.rglob("*.html"):
        if path.name != "base.html":
            assert 'src="/static/app.js"' not in path.read_text(), path


def test_base_document_requests_no_indexing_for_the_private_app(client):
    body = client.get("/").text
    assert '<meta name="robots" content="noindex, nofollow">' in body
