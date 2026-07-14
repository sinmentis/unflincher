import hashlib
import importlib.util
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "public_readiness_audit", ROOT / "tools" / "public_readiness_audit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load_audit()


def test_find_disallowed_paths_flags_private_artifacts_and_certificates():
    paths = [
        "src/app.py",
        "diary.dev.db",
        "backup.db.gz",
        "notes.xlsx",
        "chat-desktop-1440.png",
        "site/assets/images/demo-timeline.png",
        "site/assets/images/favicon.svg",
        ".superpowers/state",
        ".playwright-mcp/session.json",
        "docs/superpowers/internal-plan.md",
        ".copilot-memory.md",
        "data/private.json",
        "deploy/service.key",
        "deploy/service.pem",
        "deploy/service.crt",
        "deploy/client.p12",
        "deploy/client.pfx",
        ".env",
        ".env.production",
        ".env.example",
    ]
    result = set(audit.find_disallowed_paths(paths))
    assert {
        "diary.dev.db",
        "backup.db.gz",
        "notes.xlsx",
        "chat-desktop-1440.png",
        ".superpowers/state",
        ".playwright-mcp/session.json",
        "docs/superpowers/internal-plan.md",
        ".copilot-memory.md",
        "data/private.json",
        "deploy/service.key",
        "deploy/service.pem",
        "deploy/service.crt",
        "deploy/client.p12",
        "deploy/client.pfx",
        ".env",
        ".env.production",
    }.issubset(result)
    assert "src/app.py" not in result
    assert "site/assets/images/demo-timeline.png" not in result
    assert "site/assets/images/favicon.svg" not in result
    assert ".env.example" not in result


def test_find_unapproved_current_public_media_flags_undeclared_files():
    paths = [
        "site/assets/images/demo-timeline.png",
        "site/assets/images/favicon.svg",
        "site/assets/images/undeclared.gif",
    ]
    approved = {
        "site/assets/images/demo-timeline.png",
        "site/assets/images/favicon.svg",
    }
    assert audit.find_unapproved_public_media_paths(paths, approved) == [
        "site/assets/images/undeclared.gif"
    ]


def test_find_unapproved_historical_public_media_flags_changed_and_removed_blobs():
    approved = {"site/assets/images/demo-timeline.png": "current-sha256"}
    historical = [
        ("site/assets/images/demo-timeline.png", "current-sha256"),
        ("site/assets/images/demo-timeline.png", "older-sha256"),
        ("site/assets/images/removed.png", "removed-sha256"),
    ]
    assert audit.find_unapproved_historical_public_media(
        historical, approved
    ) == [
        "site/assets/images/demo-timeline.png: historical blob differs from approved asset",
        "site/assets/images/removed.png: historical public media is absent from the approved manifest",
    ]


def test_historical_public_media_reads_every_reachable_blob(tmp_path):
    image = tmp_path / "site" / "assets" / "images" / "demo.png"
    image.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tester@users.noreply.github.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
    )
    image.write_bytes(b"first synthetic image")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "test: add image"], cwd=tmp_path, check=True)
    image.write_bytes(b"second synthetic image")
    subprocess.run(["git", "commit", "-qam", "test: update image"], cwd=tmp_path, check=True)

    records = set(audit._historical_public_media(tmp_path))
    assert records == {
        (
            "site/assets/images/demo.png",
            hashlib.sha256(b"first synthetic image").hexdigest(),
        ),
        (
            "site/assets/images/demo.png",
            hashlib.sha256(b"second synthetic image").hexdigest(),
        ),
    }


def test_find_public_copy_issues_flags_style_claims_and_personal_email():
    files = {
        "README.md": "Source available for noncommercial use.",
        "site/index.html": "Source available for noncommercial use.",
        "site/demo/index.html": "Source available for noncommercial use.",
        "clean.md": "This is clean public copy.",
        "dash.md": "A long thought \u2014 broken by an em dash.",
        "promo.md": "The best open source journal, trusted by 10,000 users.",
        "contact.md": "Write to person@private.example for help.",
        "example.md": "Use you@example.com in this placeholder.",
    }
    issues = audit.find_public_copy_issues(files)
    assert not any(issue.startswith("clean.md") for issue in issues)
    assert not any(issue.startswith("example.md") for issue in issues)
    assert any("dash.md" in issue for issue in issues)
    assert any("promo.md" in issue for issue in issues)
    assert any("contact.md" in issue for issue in issues)


def test_find_public_copy_issues_rejects_non_english_scripts():
    assert audit.find_public_copy_issues({"clean.md": "English public copy."}) == []
    issues = audit.find_public_copy_issues({"localized.md": "\u65e5\u8bb0"})
    assert issues == ["localized.md: non-English script"]


def test_find_public_copy_issues_flags_additional_non_latin_scripts():
    scripts = {
        "greek.md": "\u03b1\u03b2",
        "arabic.md": "\u0627\u0644",
        "hebrew.md": "\u05e9\u05dc",
        "thai.md": "\u0e2a\u0e27",
        "devanagari.md": "\u0905\u0906",
        "cyrillic.md": "\u0414\u043d",
        "cjk-supplementary.md": "\U00020000",
    }
    for name, text in scripts.items():
        assert audit.find_public_copy_issues({name: text}) == [
            f"{name}: non-English script"
        ]
    assert audit.find_public_copy_issues(
        {"ascii.md": "Plain English copy with caf\u00e9 and na\u00efve, 123."}
    ) == []


def test_find_public_copy_issues_flags_emoji_foss_and_fabricated_adoption():
    files = {
        "emoji.md": "Ship it \U0001F680 today.",
        "free.md": "This is free software for everyone.",
        "foss.md": "A proud FOSS project.",
        "adopt.md": "Trusted by teams everywhere.",
    }
    issues = audit.find_public_copy_issues(files)
    assert "emoji.md: emoji" in issues
    assert "free.md: inaccurate licensing phrase" in issues
    assert "foss.md: inaccurate licensing phrase" in issues
    assert "adopt.md: fabricated claim phrase 'trusted by'" in issues


def test_find_public_copy_issues_requires_label_on_primary_surfaces():
    issues = audit.find_public_copy_issues(
        {
            "README.md": "A self-hosted journal.",
            "site/index.html": "Source available for noncommercial use.",
            "site/demo/index.html": "Source available for noncommercial use.",
        }
    )
    assert "README.md: missing source-available label" in issues


def test_find_secret_matches_detects_common_credentials():
    token = "ghp_" + "a" * 36
    pat = "github_pat_" + "b" * 30
    key = "-----BEGIN RSA " + "PRIVATE KEY-----"
    aws = "AKIA" + "B" * 16
    cloudflare = "CF_TOKEN=" + "c" * 40
    copilot = "COPILOT_GITHUB_TOKEN=" + "d" * 40
    azure = "AZURE_CLIENT_SECRET=" + "e" * 40
    findings = audit.find_secret_matches(
        {
            "f.txt": token,
            "g.txt": pat,
            "h.pem": key,
            "i.txt": aws,
            "j.txt": cloudflare,
            "k.txt": copilot,
            "l.txt": azure,
            "ok.txt": "CF_TOKEN=...",
        }
    )
    for name in ("f.txt", "g.txt", "h.pem", "i.txt", "j.txt", "k.txt", "l.txt"):
        assert any(name in item for item in findings)
    assert not any("ok.txt" in item for item in findings)


def test_find_private_term_matches_redacts_the_term_itself():
    findings = audit.find_private_term_matches(
        {
            "a.txt": "Connect to private.example.test.",
            "b.txt": "Use internal-secret-name for the unit.",
            "clean.txt": "No private term here.",
        },
        ["private.example.test", "internal-secret-name"],
    )
    assert findings == [
        "a.txt: private denylist term #1",
        "b.txt: private denylist term #2",
    ]
    assert "private.example.test" not in "\n".join(findings)


def test_find_private_term_matches_redacts_term_appearing_in_path():
    path = "docs/internal/private-host.invalid/config.md"
    findings = audit.find_private_term_matches(
        {path: path},
        ["private-host.invalid"],
    )
    assert findings == [
        "docs/internal/[redacted]/config.md: private denylist term #1"
    ]
    assert "private-host.invalid" not in "\n".join(findings)


def test_redact_private_terms_sanitizes_arbitrary_findings():
    finding = "disallowed current path: import/private-host.invalid/dump.db"
    redacted = audit._redact_private_terms(finding, ["private-host.invalid"])
    assert redacted == "disallowed current path: import/[redacted]/dump.db"
    assert "private-host.invalid" not in redacted


def test_redact_private_terms_replaces_longest_first_case_insensitively():
    text = "host reserved-node and reserved-node-01 both leak"
    redacted = audit._redact_private_terms(
        text, ["reserved-node", "reserved-node-01"]
    )
    assert redacted == "host [redacted] and [redacted] both leak"
    assert "reserved-node" not in redacted
    assert audit._redact_private_terms("Host RESERVED-NODE up", ["reserved-node"]) == (
        "Host [redacted] up"
    )


def test_find_personal_commit_emails_flags_non_noreply_addresses():
    emails = [
        "20508894+sinmentis@users.noreply.github.com",
        "223556219+Copilot@users.noreply.github.com",
        "real.person@example.com",
    ]
    assert audit.find_personal_commit_emails(emails) == ["real.person@example.com"]


def _write_denylist(path: Path, lines: list[str], mode: int = 0o600) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_load_private_terms_requires_path(monkeypatch, tmp_path):
    monkeypatch.delenv("UNFLINCHER_PUBLIC_AUDIT_DENYLIST", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    assert audit._load_private_terms(root) == (
        [],
        ["private denylist path is required"],
    )


def test_load_private_terms_rejects_in_repo_path(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    denylist = root / "denylist"
    _write_denylist(denylist, ["reserved-example-term"])
    monkeypatch.setenv("UNFLINCHER_PUBLIC_AUDIT_DENYLIST", str(denylist))
    assert audit._load_private_terms(root) == (
        [],
        ["private denylist must live outside the repository"],
    )


def test_load_private_terms_reports_missing_file(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    denylist = tmp_path / "outside" / "denylist"
    monkeypatch.setenv("UNFLINCHER_PUBLIC_AUDIT_DENYLIST", str(denylist))
    assert audit._load_private_terms(root) == (
        [],
        ["private denylist file does not exist"],
    )


def test_load_private_terms_rejects_non_0600_modes(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    for mode in (0o644, 0o400):
        denylist = outside / f"denylist-{mode:o}"
        _write_denylist(denylist, ["reserved-example-term"], mode=mode)
        monkeypatch.setenv("UNFLINCHER_PUBLIC_AUDIT_DENYLIST", str(denylist))
        assert audit._load_private_terms(root) == (
            [],
            ["private denylist must use mode 0600"],
        )


def test_load_private_terms_rejects_empty_terms(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    denylist = _write_denylist(
        outside / "denylist", ["# comment only", "   ", ""]
    )
    monkeypatch.setenv("UNFLINCHER_PUBLIC_AUDIT_DENYLIST", str(denylist))
    assert audit._load_private_terms(root) == (
        [],
        ["private denylist must contain at least one term"],
    )


def test_load_private_terms_rejects_short_terms(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    denylist = _write_denylist(outside / "denylist", ["abc"])
    monkeypatch.setenv("UNFLINCHER_PUBLIC_AUDIT_DENYLIST", str(denylist))
    assert audit._load_private_terms(root) == (
        [],
        ["private denylist terms must be at least four characters"],
    )


def test_load_private_terms_returns_deduplicated_terms(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    denylist = _write_denylist(
        outside / "denylist",
        [
            "# private terms",
            "example-host.invalid",
            "reserved-identity",
            "example-host.invalid",
            "",
        ],
    )
    monkeypatch.setenv("UNFLINCHER_PUBLIC_AUDIT_DENYLIST", str(denylist))
    terms, errors = audit._load_private_terms(root)
    assert errors == []
    assert terms == ["example-host.invalid", "reserved-identity"]
