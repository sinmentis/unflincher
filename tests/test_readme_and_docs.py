import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DOCS = ROOT / "docs"
AGENTS = ROOT / "AGENTS.md"
QUADLET = ROOT / "deploy" / "quadlet" / "unflincher.container"
DEPLOY_SCRIPT = ROOT / "deploy" / "scripts" / "deploy-unflincher.sh"

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


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_readme_leads_with_promise_label_screenshot_and_links():
    text = README.read_text(encoding="utf-8")
    head = "\n".join(text.splitlines()[:45])
    assert "evidence-grounded AI reflection partner" in head
    assert "years of journal entries" in head
    assert "dated entries" in head
    assert "challenge" in head
    assert "Source available for noncommercial use" in head
    assert "site/assets/images/demo-report.png" in head
    assert "sinmentis.github.io/unflincher/demo/" in head
    assert "docs/deployment.md" in head


def test_readme_is_clean_public_english():
    assert _problems(README.read_text(encoding="utf-8")) == []


def test_readme_links_to_all_split_docs_and_community_files():
    text = README.read_text(encoding="utf-8")
    for reference in (
        "docs/deployment.md",
        "docs/backup-and-recovery.md",
        "docs/configuration.md",
        "docs/import.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "LICENSE",
    ):
        assert reference in text


def test_readme_privacy_names_full_copilot_payload():
    low = _flat(README.read_text(encoding="utf-8")).lower()
    for phrase in ("active prompt", "journal archive", "current message", "github copilot"):
        assert phrase in low


def test_readme_describes_perspectives_supported_import_and_boundaries():
    text = _flat(README.read_text(encoding="utf-8"))
    for perspective in ("Companion", "Coach", "Challenger", "Analyst", "Custom"):
        assert perspective in text
    assert "Analyst is the default on a new database" in text
    assert "one globally active Perspective" in text
    assert "Douban diary Excel export" in text
    assert "CLI importer" in text
    assert "Write page" in text
    assert "not therapy" in text
    assert "does not diagnose or treat" in text
    assert "does not replace professional care or relationships with other people" in text
    assert "selected model's context window" in text
    assert "never silently drops older entries or Conversation history" in text


def test_readme_privacy_discloses_each_generation_path():
    text = _flat(README.read_text(encoding="utf-8"))
    for phrase in (
        "Entry Reflection sends the full Journal Archive",
        "Life Report sends the full Journal Archive",
        "General Conversation sends the full Journal Archive",
        "Entry Conversation sends the selected entry",
        "Prompt Workshop preview sends the full Journal Archive",
        "first message of a new general Conversation",
        "date title remains",
    ):
        assert phrase in text


def test_readme_describes_inert_public_demo():
    text = README.read_text(encoding="utf-8")
    assert "fictional data" in text.lower()
    assert "no model calls, tracking, cookies, storage, or writable operations" in _flat(text)


def test_readme_discloses_github_pages_logging():
    text = README.read_text(encoding="utf-8")
    assert "GitHub Pages" in text
    assert "platform logging and privacy practices" in _flat(text)


def test_readme_links_support_issue_tracker():
    text = README.read_text(encoding="utf-8")
    assert "https://github.com/sinmentis/unflincher/issues" in text


def test_repository_agent_guidance_covers_verified_public_contracts():
    text = AGENTS.read_text(encoding="utf-8")
    for phrase in (
        ".venv/bin/pytest -q",
        "nine translation catalogs",
        "English-only",
        "source-available",
        "fictional",
        "CONTRIBUTING.md",
    ):
        assert phrase in text
    assert _problems(text) == []


def test_deployment_doc_retains_key_operations():
    text = (DOCS / "deployment.md").read_text(encoding="utf-8")
    for token in (
        "podman build -t localhost/unflincher:latest",
        "cloudflared tunnel route dns",
        "create-access-unflincher-app.sh",
        "Account.Access: Apps and Policies",
        "systemctl --user",
    ):
        assert token in text
    assert _problems(text) == []


def test_public_deployment_files_use_product_scoped_secret_default():
    quadlet = QUADLET.read_text(encoding="utf-8")
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    docs = (DOCS / "deployment.md").read_text(encoding="utf-8")
    for text in (quadlet, script, docs):
        assert "unflincher-copilot-github-token" in text
        assert "diary-copilot-github-token" not in text
    assert "UNFLINCHER_COPILOT_SECRET" in script
    assert "UNFLINCHER_COPILOT_SECRET" in docs


def test_backup_doc_retains_backup_and_restore():
    text = (DOCS / "backup-and-recovery.md").read_text(encoding="utf-8")
    for token in (
        "unflincher-backup.sh",
        "unflincher-restore-drill.sh",
        "PRAGMA integrity_check",
        "timeline, report, chat, workshop, and one entry page",
    ):
        assert token in text
    assert _problems(text) == []


def test_configuration_doc_retains_env_table():
    text = (DOCS / "configuration.md").read_text(encoding="utf-8")
    for token in ("UNFLINCHER_DB", "UNFLINCHER_REQUIRE_ACCESS_AUTH", "src/unflincher/config.py"):
        assert token in text
    flat = _flat(text)
    assert "Analyst is the default Perspective for a new database." in flat
    assert "one globally active Perspective" in flat
    assert "future Entry Reflections, Life Reports, and Conversations" in flat
    assert "exactly matches a shipped preset" in flat
    assert "Custom" in flat
    assert _problems(text) == []


def test_import_doc_retains_importer_command():
    text = (DOCS / "import.md").read_text(encoding="utf-8")
    assert "import-unflincher.sh" in text
    assert ".xlsx" in text
    flat = _flat(text)
    assert "first-run guidance" in flat
    assert "CLI-only" in flat
    assert "Write page" in flat
    assert _problems(text) == []
