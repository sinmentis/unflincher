import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "unflincher" / "templates"
STATIC_JS = ROOT / "src" / "unflincher" / "static"
PAGES_CSS = STATIC_JS / "css" / "pages.css"

# Single responsive breakpoint defined in pages.css (Task 10). Mobile governs <=700px.
MOBILE_QUERY = "@media (max-width: 43.75rem)"


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
        "apply-all-btn", "regen-progress", "report-body",
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


def test_report_generation_action_follows_the_reading_body():
    source = (TEMPLATES / "report.html").read_text()
    assert source.index('id="report-body"') < source.index('id="report-stream"')
    assert source.index('id="report-stream"') < source.index('id="run-report"')
    button = re.search(
        r'<button[^>]*\bid="run-report"[^>]*>',
        source,
        re.IGNORECASE,
    )
    assert button is not None
    assert "button--accent" not in button.group(0)


def test_chat_session_relies_on_global_contextual_back_navigation():
    source = (TEMPLATES / "chat_session.html").read_text()
    topbar = (TEMPLATES / "partials" / "command_navigation.html").read_text()
    assert "mobile-chat-back" not in source
    assert 'page_id == "chat-session"' in topbar
    assert 'back_href = "/chat"' in topbar


def test_conversation_turns_are_flat_and_unnumbered():
    for name in ("entry_detail.html", "chat_session.html"):
        source = (TEMPLATES / name).read_text()
        assert '"%02d" % loop.index' not in source

    components = (STATIC_JS / "css" / "components.css").read_text()
    pages = PAGES_CSS.read_text()
    assistant = _rule_body(components, ".conversation-message.is-assistant")
    workspace_assistant = _rule_body(
        pages, ".conversation-workspace .conversation-message.is-assistant"
    )
    assert "border-left" in assistant
    assert "background" not in workspace_assistant


def test_entry_commentary_generation_uses_quiet_buttons():
    source = (TEMPLATES / "entry_detail.html").read_text()
    for button_id in ("run-commentary", "retry-commentary"):
        button = re.search(
            rf'<button[^>]*\bid="{button_id}"[^>]*>',
            source,
            re.IGNORECASE,
        )
        assert button is not None
        assert "button--accent" not in button.group(0)


def test_new_entry_title_uses_moderate_editorial_scale():
    css = PAGES_CSS.read_text()
    title = _rule_body(css, ".writing-title")
    assert "font: 500 clamp(1.875rem, 4vw, 3.125rem)" in title
    for theatrical_size in ("6rem", "15vw", "4.5rem"):
        assert theatrical_size not in css


def test_page_titles_keep_balanced_graphite_hierarchy():
    base = (STATIC_JS / "css" / "base.css").read_text()
    components = (STATIC_JS / "css" / "components.css").read_text()
    pages = PAGES_CSS.read_text()

    base_h1 = re.search(r"(?m)^h1\s*\{([^}]*)\}", base)
    assert base_h1 is not None
    assert "font-weight: 500" in base_h1.group(1)
    entry_heading = _rule_body(pages, ".entry-record .page-heading")
    assert "margin-bottom: var(--space-3)" in entry_heading
    assert "border-bottom: 0" in entry_heading
    assert "clamp(1.875rem, 4vw, 3.25rem)" in _rule_body(
        pages, ".entry-record .page-heading h1"
    )
    assert "clamp(1.375rem, 2.4vw, 1.875rem)" in _rule_body(
        pages, ".timeline-document .page-heading h1"
    )
    assert "clamp(1.375rem, 2.6vw, 1.75rem)" in _rule_body(
        pages, ".report-document .page-heading h1"
    )
    assert "clamp(1.375rem, 2.6vw, 1.75rem)" in _rule_body(
        pages, ".conversation-heading h1"
    )
    empty_title = _rule_body(components, ".empty-state h2")
    assert "clamp(1.375rem, 2.6vw, 1.75rem)" in empty_title
    assert "font-weight: 500" in empty_title


def test_primary_page_titles_do_not_repeat_matching_eyebrows():
    for name in ("timeline.html", "report.html", "workshop.html"):
        assert "eyebrow=" not in (TEMPLATES / name).read_text()
    assert '<p class="page-eyebrow">' not in (
        TEMPLATES / "chat_list.html"
    ).read_text()


def test_balanced_graphite_page_roles_exist():
    source = _template_source()
    for role in (
        "primary-task",
        "entry-body",
        "ai-commentary",
        "follow-up",
        "year-filter",
        "archive-index",
        "entry-row",
        "report-body",
        "report-history",
        "session-list",
        "session-row",
        "conversation",
        "composer",
        "entry-metadata",
        "entry-editor",
        "test-preview",
    ):
        assert f'data-role="{role}"' in source


def test_visual_contract_has_no_quiet_brutalism_artifacts():
    source = _template_source()
    css = "\n".join(
        path.read_text() for path in sorted((STATIC_JS / "css").glob("*.css"))
    )
    for forbidden in (
        "brand-seal",
        "command-navigation",
        "mobile-command-bar",
        "archive-sequence",
        "box-shadow",
        "IBM Plex",
        "#d0645a",
    ):
        assert forbidden not in source + css


def test_mobile_layouts_collapse_in_source_order():
    css = PAGES_CSS.read_text()
    assert re.findall(r"@media \(max-width: [^)]+\)", css) == [MOBILE_QUERY]
    assert not re.search(r"(?m)^\s*order\s*:", css)
    mobile = _media_block(css, MOBILE_QUERY)
    for selector in (
        ".entry-layout",
        ".timeline-layout",
        ".report-layout",
        ".chat-layout",
        ".writing-desk",
        ".workshop-layout",
    ):
        body = _rule_body(mobile, selector)
        assert "grid-template-columns: 1fr" in body
        assert not re.search(r"(?m)^\s*order\s*:", body)


def test_workshop_select_grids_can_shrink_below_option_width():
    """Regression: a <select> defaults to min-width:auto (its widest <option>), which floors its
    grid track and forces horizontal overflow -- worst on mobile with a long entry title or model
    name. The fix requires BOTH min-width:0 on the selects AND minmax(0, 1fr) tracks so the column
    can shrink below the intrinsic option width instead of overflowing the viewport."""
    css = PAGES_CSS.read_text()
    shrink = _rule_body(css, ".workshop-test-controls select")
    assert "min-width: 0" in shrink
    mobile = _media_block(css, MOBILE_QUERY)
    for selector in (".workshop-test-controls", ".workshop-model-row"):
        body = _rule_body(mobile, selector)
        assert "grid-template-columns: minmax(0, 1fr)" in body
        # A bare `1fr` track (== minmax(auto, 1fr)) would floor at the widest option and overflow.
        assert not re.search(r"grid-template-columns:\s*1fr\b", body)


def test_workshop_commit_actions_stack_cleanly_on_mobile():
    css = PAGES_CSS.read_text()
    mobile = _media_block(css, MOBILE_QUERY)
    actions = _rule_body(mobile, ".workshop-commit-actions")
    assert "flex-direction: column" in actions
    assert "align-items: stretch" in actions
    apply_all = _rule_body(mobile, ".workshop-apply-all")
    assert "padding-left: 0" in apply_all
    assert "border-left: 0" in apply_all
    assert "width: 100%" in apply_all
    button = _rule_body(mobile, ".workshop-commit-actions .button")
    assert "width: 100%" in button


def test_accessibility_fallbacks_remain_present():
    css = "\n".join(
        path.read_text() for path in sorted((STATIC_JS / "css").glob("*.css"))
    )
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (forced-colors: active)" in css
    assert ":focus-visible" in css


def _relative_luminance(hex_color: str) -> float:
    channels = [
        int(hex_color[index:index + 2], 16) / 255
        for index in (1, 3, 5)
    ]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    high, low = sorted(
        (_relative_luminance(first), _relative_luminance(second)),
        reverse=True,
    )
    return (high + 0.05) / (low + 0.05)


def test_approved_text_and_control_pairings_meet_wcag_aa():
    surfaces = ("#1d1e1d", "#222322", "#202220", "#232523", "#2c2e2c")
    for foreground in ("#e0ddd6", "#c7c2ba"):
        for background in surfaces:
            assert _contrast_ratio(foreground, background) >= 4.5
    for background in surfaces:
        assert _contrast_ratio("#85827b", background) >= 3.0

    css = "\n".join(
        path.read_text() for path in sorted((STATIC_JS / "css").glob("*.css"))
    )
    template_source = _template_source()
    interactive_classes = set()
    control_classes = set()
    for tag, attributes in re.findall(
        r"<(a|button|input|textarea|select|summary)\b([^>]*)>",
        template_source,
        flags=re.IGNORECASE,
    ):
        class_match = re.search(r"""class=["']([^"']+)["']""", attributes)
        if not class_match:
            continue
        classes = {
            name
            for name in class_match.group(1).split()
            if re.fullmatch(r"[A-Za-z_][\w-]*", name)
        }
        interactive_classes.update(classes)
        if tag.lower() in {"button", "input", "textarea", "select"}:
            control_classes.update(classes)

    semantic_interactive = re.compile(
        r"(^|[\s>+~,(:])(?:a|button|input|textarea|select|summary)\b",
        flags=re.IGNORECASE,
    )
    semantic_control = re.compile(
        r"(^|[\s>+~,(:])(?:button|input|textarea|select)\b",
        flags=re.IGNORECASE,
    )
    muted_text = re.compile(
        r"(?:^|;)\s*color\s*:\s*var\(--muted\)",
        flags=re.IGNORECASE,
    )
    border_using_rule = re.compile(
        r"border(?:-[a-z-]+)?\s*:[^;]*var\(--rule\)",
        flags=re.IGNORECASE,
    )
    boundary_using_rule = re.compile(
        r"border(?:-color)?\s*:[^;]*var\(--rule\)",
        flags=re.IGNORECASE,
    )
    flat_css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for match in re.finditer(r"([^{}]*)\{([^{}]*)\}", flat_css):
        selectors, declarations = match.groups()
        assert not muted_text.search(declarations), selectors
        has_interactive_class = any(
            re.search(rf"\.{re.escape(name)}(?![\w-])", selectors)
            for name in interactive_classes
        )
        has_control_class = any(
            re.search(rf"\.{re.escape(name)}(?![\w-])", selectors)
            for name in control_classes
        )
        if semantic_control.search(selectors) or has_control_class:
            assert not border_using_rule.search(declarations), selectors
        elif semantic_interactive.search(selectors) or has_interactive_class:
            assert not boundary_using_rule.search(declarations), selectors


def test_writing_planes_keep_the_approved_baseline_texture():
    css = PAGES_CSS.read_text()
    assert "repeating-linear-gradient" in css


def test_legacy_css_tokens_are_removed():
    css = "\n".join(
        path.read_text() for path in sorted((STATIC_JS / "css").glob("*.css"))
    )
    tokens = (STATIC_JS / "css" / "tokens.css").read_text()
    for legacy in (
        "canvas",
        "surface",
        "surface-raised",
        "ink",
        "ink-soft",
        "accent",
        "font-command",
        "font-reading",
        "font-mono",
        "header-height",
        "radius-1",
        "radius-2",
        "motion-fast",
        "motion-default",
        "z-base",
        "z-sticky",
        "z-header",
        "z-overlay",
    ):
        assert f"var(--{legacy})" not in css
        assert f"--{legacy}:" not in tokens


def test_perspective_indicator_is_one_shared_partial_reused_by_all_three_surfaces():
    """Entry Reflection, Life Report, and Conversation composers must share ONE partial/CSS
    class for the active/historical Perspective indicator rather than three ad-hoc labels."""
    partial = (TEMPLATES / "partials" / "perspective_indicator.html").read_text()
    assert 'data-role="perspective-indicator"' in partial
    assert 'class="perspective-indicator"' in partial

    entry_detail = (TEMPLATES / "entry_detail.html").read_text()
    report = (TEMPLATES / "report.html").read_text()
    composer = (TEMPLATES / "partials" / "conversation_composer.html").read_text()
    for source in (entry_detail, report, composer):
        assert 'partials/perspective_indicator.html' in source

    css = "\n".join(
        path.read_text() for path in sorted((STATIC_JS / "css").glob("*.css"))
    )
    assert ".perspective-indicator" in css
    # Declared exactly once -- one shared rule, not per-surface duplicates.
    assert css.count(".perspective-indicator {") == 1


def test_conversation_messages_never_carry_per_turn_perspective_badges():
    """Non-goal: no per-message Perspective badges or historical message labels -- only the
    composer shows a forward-looking indicator, and only Entry Reflection/Life Report show a
    per-version indicator. Individual conversation-message turns must stay unlabeled."""
    for name in ("entry_detail.html", "chat_session.html"):
        source = (TEMPLATES / name).read_text()
        message_loop_start = source.index("conversation-message")
        message_loop_end = source.index("{% endfor %}", message_loop_start)
        turn_markup = source[message_loop_start:message_loop_end]
        assert "perspective" not in turn_markup.lower()
