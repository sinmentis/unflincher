import hashlib
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
DASHES = ("\u2014", "\u2013")
NON_ENGLISH_SCRIPT = re.compile("[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0400-\u04ff]")


def _problems(text: str) -> list[str]:
    problems = []
    if any(dash in text for dash in DASHES):
        problems.append("em or en dash")
    if EMOJI.search(text):
        problems.append("emoji")
    if NON_ENGLISH_SCRIPT.search(text):
        problems.append("non-English script")
    if "open source" in text.lower() or "open-source" in text.lower():
        problems.append("open source phrase")
    return problems


def _personal_emails(text: str) -> list[str]:
    return [email for email in EMAIL.findall(text) if not email.endswith("users.noreply.github.com")]


def test_security_uses_private_reporting_not_personal_email():
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "private vulnerability reporting" in text.lower()
    assert "mailto:" not in text
    assert _personal_emails(text) == []
    assert _problems(text) == []


def test_contributing_covers_setup_tests_and_license():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert ".[dev]" in text
    assert "pytest" in text
    assert "noncommercial" in text.lower()
    assert _problems(text) == []


def test_code_of_conduct_exists_without_personal_email():
    text = (ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
    assert _personal_emails(text) == []
    assert _problems(text) == []


def test_changelog_and_release_notes_freeze_v0_2_0():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = (ROOT / "docs" / "release-notes-v0.2.0.md").read_text(encoding="utf-8")
    assert changelog.index("0.2.0 (2026-07-16)") < changelog.index("0.1.0")
    assert "0.2.0" in notes
    assert "Prepared: 2026-07-16. Not published." in notes
    assert "An existing v0.1 database must use the fail-locked procedure" in notes
    assert "[upgrade-v0.2.md](upgrade-v0.2.md)" in notes
    for phrase in (
        "evidence-grounded AI reflection partner",
        "Companion",
        "Coach",
        "Challenger",
        "Analyst",
        "Custom",
        "Entry Reflection",
        "Life Report",
        "Conversation",
        "Prompt Workshop",
        "selected model's context window",
        "never silently drops",
        "maintenance",
        "request fingerprint",
        "Douban diary Excel export",
        "not therapy",
    ):
        assert phrase in notes
    assert "noncommercial" in notes.lower()
    assert "local SQLite" in notes
    assert "GitHub Pages" in notes
    assert _problems(changelog) == []
    assert _problems(notes) == []


def test_v0_1_0_release_notes_remain_historical_and_unchanged():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes_path = ROOT / "docs" / "release-notes-v0.1.0.md"
    notes = notes_path.read_text(encoding="utf-8")
    assert "0.1.0" in changelog
    assert "0.1.0" in notes
    assert _problems(notes) == []
    assert hashlib.sha256(notes_path.read_bytes()).hexdigest() == (
        "2ea4948c30171709d4c8871113badb1222e0c5ad33d64413d524b2a00fc8cd91"
    )


def test_readme_links_changelog_release_notes_and_support():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "CHANGELOG.md" in text
    assert "docs/release-notes-v0.2.0.md" in text
    assert "docs/release-notes-v0.1.0.md" in text
    assert "https://github.com/sinmentis/unflincher/discussions" in text


def test_issue_and_pr_templates_exist_with_required_checks():
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").is_file()
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.md").is_file()
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").is_file()
    pr = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8").lower()
    assert "test" in pr
    assert "privacy" in pr
    assert "public data" in pr
    assert "real diary content" in pr
    assert "tokens" in pr
    assert "production hostnames" in pr
    assert "private databases" in pr
    assert "synthetic" in pr


def test_issue_templates_warn_against_real_private_data():
    bug = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").read_text(encoding="utf-8")
    feature = (ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.md").read_text(encoding="utf-8")

    for text in (bug, feature):
        lowered = text.lower()
        assert "## privacy check" in lowered
        assert "real diary content" in lowered
        assert "tokens" in lowered
        assert "production hostnames" in lowered
        assert "fictional examples" in lowered
        assert _problems(text) == []

    assert "private databases" in bug.lower()
    assert "private databases" in feature.lower()


def test_gitleaks_ignore_allows_only_verified_false_positive():
    text = (ROOT / ".gitleaksignore").read_text(encoding="utf-8")
    fingerprints = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "false positive" in text.lower()
    assert fingerprints == [
        "731d340b7935199a4652f302c6fb7a3693c91161:"
        "src/unflincher/context_budget.py:generic-api-key:113"
    ]


def test_pyproject_metadata_is_enriched_without_osi_classifier():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["version"] == "0.2.0"
    assert project["description"] == (
        "Evidence-grounded AI reflection partner for finding patterns across years of journal entries."
    )
    assert project["readme"] == "README.md"
    assert {"reflection", "journal-analysis", "ai-reflection-partner"}.issubset(
        project["keywords"]
    )
    assert "Homepage" in project["urls"]
    for classifier in project.get("classifiers", []):
        assert "OSI Approved" not in classifier
