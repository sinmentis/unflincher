import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "unflincher" / "templates"
STATIC_JS = ROOT / "src" / "unflincher" / "static"


def _template_source() -> str:
    return "\n".join(path.read_text() for path in TEMPLATES.rglob("*.html"))


def test_templates_have_no_native_dialogs_or_emoji_controls():
    source = _template_source()
    for forbidden in ("alert(", "prompt(", "confirm(", "✎", "🗑"):
        assert forbidden not in source
    browser_source = (STATIC_JS / "app.js").read_text() + "\n".join(
        path.read_text() for path in (STATIC_JS / "js").glob("*.js")
    )
    for forbidden in ("alert(", "prompt(", "confirm("):
        assert forbidden not in browser_source


def test_templates_have_no_executable_inline_scripts():
    source = _template_source()
    inline_script = re.compile(
        r"<script(?![^>]*\bsrc=)(?![^>]*\btype=[\"']application/json[\"'])[^>]*>",
        re.IGNORECASE,
    )
    assert not inline_script.search(source)


def test_templates_have_no_layout_styles_or_event_handler_attributes():
    source = _template_source()
    assert not re.search(r"\sstyle\s*=", source, re.IGNORECASE)
    assert not re.search(r"\son[a-z]+\s*=", source, re.IGNORECASE)


def test_stable_dom_hooks_remain_in_template_sources():
    source = _template_source()
    for hook in (
        "diary-text", "ai-commentary", "chat-section", "chat-thread", "chat-stream",
        "chat-input", "chat-send", "run-commentary", "retry-commentary",
        "commentary-status", "report-stream", "run-report", "prompt-draft",
        "model-select", "test-entry", "run-test", "preview-stream", "apply-btn",
        "apply-all-btn", "regen-progress",
    ):
        assert f'id="{hook}"' in source


def test_page_templates_use_semantic_landmarks():
    for name in ("timeline.html", "entry_detail.html", "report.html", "chat_list.html",
                 "chat_session.html", "new_entry.html", "workshop.html"):
        source = (TEMPLATES / name).read_text()
        assert any(tag in source for tag in ("<article", "<section"))


def test_legacy_theme_and_page_inline_scripts_are_removed():
    assert not (STATIC_JS / "theme.css").exists()
    assert 'data-legacy-theme' not in _template_source()
    for name in ("timeline.js", "entry.js", "report.js", "chat.js", "new-entry.js", "workshop.js"):
        assert (STATIC_JS / "js" / name).is_file()


def test_legacy_component_selectors_are_removed():
    source = _template_source()
    for selector in ('class="ai-card', 'class="badge', 'class="side-nav',
                     'class="chat-bubble', 'class="entry-date', 'class="ws-section'):
        assert selector not in source
