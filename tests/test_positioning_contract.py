import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "CONTEXT.md"
ADR = ROOT / "docs" / "adr" / "0001-reflection-partner-and-global-perspective.md"
TEMPLATES = ROOT / "src" / "unflincher" / "templates"
STATIC_CSS = ROOT / "src" / "unflincher" / "static" / "css"

DOMAIN_TERMS = (
    "Reflection Partner",
    "Journal Archive",
    "Entry Reflection",
    "Life Report",
    "Conversation",
    "Perspective",
    "Companion",
    "Coach",
    "Challenger",
    "Analyst",
    "Custom",
    "Prompt Workshop",
    "Entry Reference",
)


def _clean_public_copy(text: str) -> None:
    assert "\u2013" not in text
    assert "\u2014" not in text
    assert not re.search("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]", text)
    assert "open source" not in text.lower()
    assert "open-source" not in text.lower()


def test_context_defines_the_canonical_domain_language():
    text = CONTEXT.read_text(encoding="utf-8")
    for term in DOMAIN_TERMS:
        assert f"**{term}**" in text
    assert "AI Mentor" not in text
    assert "Prompt Workshop" in text
    _clean_public_copy(text)


def test_adr_locks_the_repositioning_and_behavioral_decisions():
    text = ADR.read_text(encoding="utf-8")
    required = (
        "evidence-grounded AI reflection partner",
        "one globally active Perspective",
        "Analyst",
        "existing active prompt",
        "Custom",
        "Prompt Workshop",
        "CLI-only",
        "not therapy, diagnosis, or treatment",
    )
    for phrase in required:
        assert phrase in text
    assert "Per-conversation Perspective" in text
    assert "In-app Excel upload" in text
    _clean_public_copy(text)


def test_application_uses_the_canonical_english_product_language():
    from unflincher.i18n import TRANSLATIONS

    english = TRANSLATIONS["en"]
    expected = {
        "nav.title": "Unflincher: Reflect on your journal",
        "nav.chat": "Conversations",
        "nav.new_entry": "Write",
        "nav.workshop": "Prompt Workshop",
        "entry.toc_commentary": "Entry Reflection",
        "entry.toc_chat": "Conversation",
        "entry.no_commentary_yet": "No reflection yet.",
        "entry.run_commentary_button": "Generate reflection",
        "chat.heading": "Conversations",
        "workshop.heading": "Prompt Workshop",
        "common.mentor": "Unflincher",
    }
    for key, value in expected.items():
        assert english[key] == value

    rendered_copy = "\n".join(english.values())
    for forbidden in ("AI Mentor", "AI Commentary", "Prompt Settings", "New Entry", "Chat"):
        assert forbidden not in rendered_copy


def test_all_languages_use_unflincher_as_the_assistant_name():
    from unflincher.i18n import TRANSLATIONS

    for catalog in TRANSLATIONS.values():
        assert catalog["common.mentor"] == "Unflincher"

    rendered_copy = "\n".join(
        value
        for catalog in TRANSLATIONS.values()
        for value in catalog.values()
    ).lower()
    for forbidden in (
        "ai mentor",
        "mentor ia",
        "ki-mentor",
        "ии-наставник",
        "aiメンター",
        "ai 멘토",
        "ai 人生导师",
        "mentor de ia",
    ):
        assert forbidden not in rendered_copy


def test_assistant_message_css_uses_role_language_not_mentor_language():
    source = "\n".join(path.read_text() for path in TEMPLATES.rglob("*.html"))
    css = "\n".join(path.read_text() for path in STATIC_CSS.glob("*.css"))

    assert "is-mentor" not in source + css
    assert "is-assistant" in source
    assert ".conversation-message.is-assistant" in css
