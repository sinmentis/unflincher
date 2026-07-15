import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "CONTEXT.md"
ADR = ROOT / "docs" / "adr" / "0001-reflection-partner-and-global-perspective.md"

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
