from pathlib import Path

import pytest

# PyYAML ships with the repo transitively (via uvicorn[standard]); require it
# because these guards are the workflow-publication safety contract and must
# actually run rather than silently skip.
yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
PAGES = ROOT / ".github" / "workflows" / "pages.yml"

# Terms that must never appear in the Pages workflow. Publishing the static
# site must not reach for secrets, container tooling, the production host, any
# remote-copy transport, or the private database. Matched case-insensitively as
# substrings.
PAGES_FORBIDDEN_TERMS = (
    "secrets.",
    "podman",
    "cloudflare",
    "cloudflared",
    "unflincher.yourdomain.com",
    "ssh",
    "scp",
    "rsync",
    "database",
    "unflincher.db",
)


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(path):
    """Return the ``on:`` trigger mapping of a workflow as a dict.

    PyYAML implements YAML 1.1, where the bare key ``on`` is resolved to the
    boolean ``True`` rather than the string ``"on"``. Accept either spelling so
    the assertion is robust across loader versions.
    """
    data = _load(path)
    for key in (True, "on"):
        if key in data:
            triggers = data[key]
            assert isinstance(triggers, dict), f"`on:` must be a mapping in {path.name}"
            return triggers
    raise AssertionError(f"no `on:` trigger mapping found in {path.name}")


def _steps(path, job):
    return _load(path)["jobs"][job]["steps"]


def _step_using(steps, action_prefix):
    matches = [s for s in steps if str(s.get("uses", "")).startswith(action_prefix)]
    assert matches, f"expected a step using {action_prefix!r}"
    return matches[0]


def test_ci_uses_pinned_actions_and_runs_pytest():
    text = CI.read_text(encoding="utf-8")
    assert "actions/checkout@v6" in text
    assert "actions/setup-python@v6" in text
    assert "pytest -q" in text


def test_ci_triggers_are_exactly_push_and_pull_request():
    assert set(_triggers(CI)) == {"push", "pull_request"}


def test_ci_permissions_are_least_privilege():
    assert _load(CI)["permissions"] == {"contents": "read"}


def test_ci_uses_python_312_and_installs_dev_extra():
    steps = _steps(CI, "test")
    setup = _step_using(steps, "actions/setup-python@")
    assert str(setup["with"]["python-version"]) == "3.12"
    run_commands = " ".join(s.get("run", "") for s in steps)
    assert ".[dev]" in run_commands


def test_pages_uses_pinned_actions_and_uploads_site():
    text = PAGES.read_text(encoding="utf-8")
    assert "actions/checkout@v6" in text
    assert "actions/configure-pages@v5" in text
    assert "actions/upload-pages-artifact@v4" in text
    assert "actions/deploy-pages@v4" in text
    assert "path: site" in text


def test_pages_triggers_are_manual_only():
    # Exact-set match: any auto trigger (push, pull_request, schedule,
    # workflow_run, release, ...) added alongside workflow_dispatch fails here.
    assert set(_triggers(PAGES)) == {"workflow_dispatch"}


def test_pages_permissions_are_exact():
    assert _load(PAGES)["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }


def test_pages_deploy_environment_is_github_pages():
    deploy = _load(PAGES)["jobs"]["deploy"]
    assert deploy["environment"]["name"] == "github-pages"
    assert deploy["environment"]["url"] == "${{ steps.deployment.outputs.page_url }}"


def test_pages_uploads_only_the_static_site_directory():
    upload = _step_using(_steps(PAGES, "build"), "actions/upload-pages-artifact@")
    assert upload["with"]["path"] == "site"


def test_pages_workflow_has_no_forbidden_publication_terms():
    lowered = PAGES.read_text(encoding="utf-8").lower()
    for term in PAGES_FORBIDDEN_TERMS:
        assert term not in lowered, f"forbidden term {term!r} present in pages.yml"


def test_workflows_are_valid_yaml():
    for path in (CI, PAGES):
        _load(path)
