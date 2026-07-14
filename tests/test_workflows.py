from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
PAGES = ROOT / ".github" / "workflows" / "pages.yml"


def test_ci_runs_the_test_suite_on_push_and_pull_request():
    text = CI.read_text(encoding="utf-8")
    assert "actions/checkout@v6" in text
    assert "actions/setup-python@v6" in text
    assert "pytest -q" in text
    assert "push:" in text
    assert "pull_request:" in text
    assert "contents: read" in text


def test_pages_workflow_is_manual_only_and_uploads_site():
    text = PAGES.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "push:" not in text
    assert "pull_request:" not in text
    assert "actions/checkout@v6" in text
    assert "actions/configure-pages@v5" in text
    assert "actions/upload-pages-artifact@v4" in text
    assert "actions/deploy-pages@v4" in text
    assert "path: site" in text
    assert "secrets." not in text
    assert "podman" not in text.lower()
    assert "cloudflared" not in text.lower()


def test_pages_workflow_has_pages_permissions_and_environment():
    text = PAGES.read_text(encoding="utf-8")
    assert "pages: write" in text
    assert "id-token: write" in text
    assert "github-pages" in text


def test_workflows_are_valid_yaml():
    yaml = pytest.importorskip("yaml")
    for path in (CI, PAGES):
        yaml.safe_load(path.read_text(encoding="utf-8"))
