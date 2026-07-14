import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "unflincher" / "templates"
STATIC_JS = ROOT / "src" / "unflincher" / "static"
PAGES_CSS = STATIC_JS / "css" / "pages.css"

# Responsive breakpoint defined in pages.css (Task 10). Mobile governs <=767px.
MOBILE_QUERY = "@media (max-width: 47.9375rem)"


def _template_source() -> str:
    return "\n".join(path.read_text() for path in TEMPLATES.rglob("*.html"))


def _media_block(css: str, query: str) -> str:
    """Return the declarations inside the (single) `@media <query> { ... }` block."""
    start = css.index(query)
    open_brace = css.index("{", start)
    depth = 0
    for index in range(open_brace, len(css)):
        char = css[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css[open_brace + 1:index]
    raise AssertionError(f"unbalanced braces after {query!r}")


def _rule_body(block: str, selector: str) -> str:
    """Return the declaration body of the rule whose selector list contains `selector` exactly."""
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
    for match in re.finditer(r"([^{}]*)\{([^{}]*)\}", block):
        selectors = [part.strip() for part in match.group(1).split(",")]
        if selector in selectors:
            return match.group(2)
    raise AssertionError(f"no rule for selector {selector!r} in media block")


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


def test_timeline_archive_no_longer_depends_on_sequence_numbers():
    source = (TEMPLATES / "timeline.html").read_text()
    css = PAGES_CSS.read_text()
    assert "archive-sequence" not in source
    assert ".archive-sequence" not in css
    assert 'data-role="entry-row"' in source


def test_archive_row_hover_has_no_horizontal_motion():
    """The Balanced Graphite design prohibits decorative horizontal row translation. The
    `.archive-row:hover` rule may keep its quiet background tint but must not translate the row
    or declare a transform transition."""
    css = PAGES_CSS.read_text()
    hover_body = _rule_body(css, ".archive-row:hover")
    assert "transform" not in hover_body, f".archive-row:hover must not translate the row: {hover_body!r}"
    row_body = _rule_body(css, ".archive-row")
    assert "transform" not in row_body, f".archive-row must not transition transform: {row_body!r}"


def test_report_history_follows_report_body_in_source_order():
    source = (TEMPLATES / "report.html").read_text()
    assert source.index('data-role="report-body"') < source.index(
        'data-role="report-history"'
    )


def test_mobile_session_ledger_drops_right_divider():
    """When chat collapses to a single mobile column the session ledger fills the width, so the
    desktop `border-right` becomes a stray divider on the right edge and must be removed."""
    mobile = _media_block(PAGES_CSS.read_text(), MOBILE_QUERY)
    body = _rule_body(mobile, ".session-ledger")
    assert re.search(r"border-right:\s*(0|none)\b", body), f"mobile .session-ledger must drop its right border: {body!r}"
