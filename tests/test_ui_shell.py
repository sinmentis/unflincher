from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import unflincher.llm as llm_module
from unflincher.app import create_app
from unflincher.templates_env import get_ui_state

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "unflincher" / "templates"


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


def test_base_document_has_balanced_graphite_metadata_and_landmarks(client):
    body = client.get("/").text
    assert '<meta name="theme-color" content="#1d1e1d">' in body
    assert 'class="app-topbar"' in body
    assert 'class="quiet-nav"' in body
    assert 'class="quiet-nav-panel"' in body
    assert 'id="main-content"' in body
    assert 'data-page="timeline"' in body
    assert 'data-nav="timeline"' in body
    assert 'aria-current="page"' in body
    assert "UNFLINCHER" in body
    assert "brand-seal" not in body


def test_quiet_menu_exposes_each_destination_once(client):
    body = client.get("/chat").text
    for key in ("timeline", "report", "chat", "new_entry", "workshop"):
        assert body.count(f'data-nav="{key}"') == 1
    assert body.count('class="quiet-nav-panel"') == 1
    assert 'data-page="chat-list"' in body


def test_topbar_back_link_follows_page_context(client):
    assert 'class="topbar-back"' not in client.get("/").text
    assert 'class="topbar-back" href="/"' in client.get("/new").text
    assert 'class="topbar-back" href="/chat"' in client.get("/chat/new").text


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
