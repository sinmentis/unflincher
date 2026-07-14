"""Local public-readiness audit for the Unflincher launch preparation.

The audit scans the publishable current tree and Git history, validates public copy,
checks current and historical paths, detects common credential forms, verifies
synthetic fixture and image provenance, compares historical public-media blob hashes
with the approved manifest, and compares all text with a private denylist.
Known private identifiers are never embedded in this committed source file."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import unicodedata
from pathlib import Path

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
DASHES = ("\u2014", "\u2013")
EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
REDACTION_MARKER = "[redacted]"

SECRET_PATTERNS = {
    "github-oauth-token": re.compile(r"gh[opusr]_[A-Za-z0-9]{36,}"),
    "github-pat": re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    "private-key-block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "cloudflare-token-assignment": re.compile(
        r"(?i)\b(?:CF_TOKEN|CLOUDFLARE_API_TOKEN)\s*=\s*['\"]?[A-Za-z0-9_-]{20,}"
    ),
    "copilot-token-assignment": re.compile(
        r"(?i)\bCOPILOT_GITHUB_TOKEN\s*=\s*['\"]?[A-Za-z0-9_-]{20,}"
    ),
    "azure-client-secret-assignment": re.compile(
        r"(?i)\bAZURE_CLIENT_SECRET\s*=\s*['\"]?[A-Za-z0-9_./+=-]{20,}"
    ),
}

DISALLOWED_SUFFIXES = (
    ".db",
    ".db.gz",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
    ".sqlite-wal",
    ".sqlite-shm",
    ".xls",
    ".xlsx",
    ".key",
    ".pem",
    ".crt",
    ".cer",
    ".p8",
    ".p12",
    ".pfx",
    ".jks",
    ".jceks",
    ".keystore",
)
DISALLOWED_PREFIXES = (
    ".superpowers/",
    ".playwright-mcp/",
    "docs/superpowers/",
    "import/",
    "data/",
    "backup/",
    "backups/",
)
PRIVATE_MEDIA_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov")
PUBLIC_MEDIA_SUFFIXES = PRIVATE_MEDIA_SUFFIXES + (".svg",)
PUBLIC_IMAGE_PREFIX = "site/assets/images/"

PUBLIC_TEXT_SUFFIXES = {
    ".html",
    ".css",
    ".js",
    ".json",
    ".xml",
    ".txt",
    ".md",
    ".yml",
    ".yaml",
    ".toml",
}
PUBLIC_ROOT_FILES = {
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "pyproject.toml",
}
REQUIRED_SOURCE_LABELS = {"README.md", "site/index.html", "site/demo/index.html"}
SOURCE_LABEL_PHRASES = (
    "source available for noncommercial use",
    "source-available",
    "polyform noncommercial",
)
FABRICATED_CLAIMS = (
    "trusted by",
    "10,000 users",
    "best-in-class",
    "industry-leading",
    "customer logos",
)


def find_disallowed_paths(paths: list[str]) -> list[str]:
    flagged: list[str] = []
    for path in paths:
        name = path.replace("\\", "/")
        lowered = name.lower()
        basename = Path(lowered).name
        if lowered.endswith(DISALLOWED_SUFFIXES):
            flagged.append(path)
        elif basename == ".copilot-memory.md":
            flagged.append(path)
        elif basename == ".env" or (
            basename.startswith(".env.") and basename != ".env.example"
        ):
            flagged.append(path)
        elif lowered.startswith(DISALLOWED_PREFIXES):
            flagged.append(path)
        elif lowered.endswith(PRIVATE_MEDIA_SUFFIXES) and not lowered.startswith(
            PUBLIC_IMAGE_PREFIX
        ):
            flagged.append(path)
    return flagged


def find_unapproved_public_media_paths(
    paths: list[str], approved_paths: set[str]
) -> list[str]:
    approved = {path.replace("\\", "/").lower() for path in approved_paths}
    return sorted(
        path
        for path in paths
        if (normalized := path.replace("\\", "/").lower()).startswith(
            PUBLIC_IMAGE_PREFIX
        )
        and normalized.endswith(PUBLIC_MEDIA_SUFFIXES)
        and normalized not in approved
    )


def find_unapproved_historical_public_media(
    historical_media: list[tuple[str, str]],
    approved_sha256: dict[str, str],
) -> list[str]:
    issues: set[str] = set()
    approved = {
        path.replace("\\", "/").lower(): digest
        for path, digest in approved_sha256.items()
    }
    for path, digest in historical_media:
        normalized = path.replace("\\", "/").lower()
        expected = approved.get(normalized)
        if expected is None:
            issues.add(
                f"{path}: historical public media is absent from the approved manifest"
            )
        elif digest != expected:
            issues.add(
                f"{path}: historical blob differs from approved asset"
            )
    return sorted(issues)


def _has_non_english_script(text: str) -> bool:
    """Return True when text contains a letter or mark from a non-Latin script."""
    for char in text:
        if char.isascii():
            continue
        if unicodedata.category(char)[0] not in ("L", "M"):
            continue
        if not unicodedata.name(char, "").startswith("LATIN"):
            return True
    return False


def find_public_copy_issues(files: dict[str, str]) -> list[str]:
    issues: list[str] = []
    for name, text in files.items():
        if any(dash in text for dash in DASHES):
            issues.append(f"{name}: em or en dash")
        if EMOJI.search(text):
            issues.append(f"{name}: emoji")
        if _has_non_english_script(text):
            issues.append(f"{name}: non-English script")
        lowered = text.lower()
        if (
            "open source" in lowered
            or "open-source" in lowered
            or "free software" in lowered
            or re.search(r"\bfoss\b", lowered)
        ):
            issues.append(f"{name}: inaccurate licensing phrase")
        for claim in FABRICATED_CLAIMS:
            if claim in lowered:
                issues.append(f"{name}: fabricated claim phrase {claim!r}")
        for email in EMAIL.findall(text):
            if not (
                email.lower().endswith("@example.com")
                or email.lower().endswith("users.noreply.github.com")
            ):
                issues.append(f"{name}: personal email address")
    for name in sorted(REQUIRED_SOURCE_LABELS):
        text = files.get(name)
        if text is not None and not any(
            phrase in text.lower() for phrase in SOURCE_LABEL_PHRASES
        ):
            issues.append(f"{name}: missing source-available label")
    return issues


def find_secret_matches(files: dict[str, str]) -> list[str]:
    findings: list[str] = []
    for name, text in files.items():
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{name}: {label}")
    return findings


def _redact_private_terms(text: str, terms: list[str]) -> str:
    """Replace every private term with a neutral marker, longest terms first."""
    for term in sorted((term for term in terms if term), key=len, reverse=True):
        text = re.sub(
            re.escape(term),
            lambda _match: REDACTION_MARKER,
            text,
            flags=re.IGNORECASE,
        )
    return text


def find_private_term_matches(
    files: dict[str, str], terms: list[str]
) -> list[str]:
    findings: list[str] = []
    for name, text in files.items():
        display = _redact_private_terms(name, terms)
        lowered = text.lower()
        for index, term in enumerate(terms, start=1):
            if term.lower() in lowered:
                findings.append(f"{display}: private denylist term #{index}")
    return findings


def find_personal_commit_emails(emails: list[str]) -> list[str]:
    return [
        email
        for email in emails
        if email and not email.lower().endswith("users.noreply.github.com")
    ]


def _git_lines(args: list[str], root: Path) -> list[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()


def _git_text(args: list[str], root: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    ).stdout


def _historical_public_media(root: Path) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    for line in _git_lines(
        ["rev-list", "--objects", "--all", "--", "site/assets/images"],
        root,
    ):
        object_id, separator, rel = line.partition(" ")
        normalized = rel.replace("\\", "/").lower()
        if not separator or not normalized.endswith(PUBLIC_MEDIA_SUFFIXES):
            continue
        content = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout
        records.append((rel, hashlib.sha256(content).hexdigest()))
    return records


def _read_text(root: Path, rel: str) -> str | None:
    try:
        content = (root / rel).read_bytes()
    except OSError:
        return None
    if b"\x00" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_public_text(rel: str) -> bool:
    if rel in PUBLIC_ROOT_FILES:
        return True
    if rel.startswith("site/") and not rel.startswith("site/assets/fonts/"):
        return Path(rel).suffix in PUBLIC_TEXT_SUFFIXES
    if rel.startswith("docs/") and rel.endswith(".md"):
        return True
    if rel.startswith(".github/"):
        return True
    return False


def _load_private_terms(root: Path) -> tuple[list[str], list[str]]:
    path_text = os.environ.get("UNFLINCHER_PUBLIC_AUDIT_DENYLIST", "").strip()
    if not path_text:
        return [], ["private denylist path is required"]
    path = Path(path_text).expanduser().resolve()
    if path == root or root in path.parents:
        return [], ["private denylist must live outside the repository"]
    if not path.is_file():
        return [], ["private denylist file does not exist"]
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        return [], ["private denylist must use mode 0600"]
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            terms.append(value)
    terms = list(dict.fromkeys(terms))
    if not terms:
        return [], ["private denylist must contain at least one term"]
    if any(len(term) < 4 for term in terms):
        return [], ["private denylist terms must be at least four characters"]
    return terms, []


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[1]
    findings: list[str] = []

    current_paths = sorted(
        set(
            _git_lines(
                ["ls-files", "--cached", "--others", "--exclude-standard"],
                root,
            )
        )
    )
    historical_paths = sorted(
        {
            line
            for line in _git_lines(
                ["log", "--all", "--name-only", "--format="],
                root,
            )
            if line
        }
    )
    findings += [
        f"disallowed current path: {path}"
        for path in find_disallowed_paths(current_paths)
    ]
    findings += [
        f"disallowed historical path: {path}"
        for path in find_disallowed_paths(historical_paths)
    ]

    current_text: dict[str, str] = {}
    public_text: dict[str, str] = {}
    for rel in current_paths:
        text = _read_text(root, rel)
        if text is None:
            continue
        current_text[rel] = text
        if _is_public_text(rel):
            public_text[rel] = text

    history_patch = _git_text(
        ["log", "-p", "--all", "--full-history", "--no-ext-diff", "--", "."],
        root,
    )
    history_text = {"<git-history>": history_patch}

    findings += [
        f"public copy issue: {issue}"
        for issue in find_public_copy_issues(public_text)
    ]
    findings += [
        f"possible current-tree secret: {item}"
        for item in find_secret_matches(current_text)
    ]
    findings += [
        f"possible historical secret: {item}"
        for item in find_secret_matches(history_text)
    ]

    private_terms, denylist_errors = _load_private_terms(root)
    findings += [f"denylist: {error}" for error in denylist_errors]
    if private_terms:
        findings += [
            f"private current-path identifier: {item}"
            for item in find_private_term_matches(
                {path: path for path in current_paths},
                private_terms,
            )
        ]
        findings += [
            f"private historical-path identifier: {item}"
            for item in find_private_term_matches(
                {path: path for path in historical_paths},
                private_terms,
            )
        ]
        findings += [
            f"private current-tree identifier: {item}"
            for item in find_private_term_matches(current_text, private_terms)
        ]
        findings += [
            f"private historical identifier: {item}"
            for item in find_private_term_matches(history_text, private_terms)
        ]

    emails = sorted(
        set(_git_lines(["log", "--all", "--format=%ae%n%ce"], root))
    )
    findings += [
        f"personal commit email: {email}"
        for email in find_personal_commit_emails(emails)
    ]

    provenance = _load_module(
        root / "tools" / "public_provenance.py",
        "public_provenance",
    )
    images = root / "site" / "assets" / "images"
    manifest = json.loads(
        (images / "provenance.json").read_text(encoding="utf-8")
    )
    approved_public_media = {
        f"{PUBLIC_IMAGE_PREFIX}{entry['file']}": entry["sha256"]
        for entry in manifest
        if isinstance(entry.get("file"), str)
        and isinstance(entry.get("sha256"), str)
    }
    findings += [
        f"unapproved current public media: {path}"
        for path in find_unapproved_public_media_paths(
            current_paths,
            set(approved_public_media),
        )
    ]
    findings += [
        f"unapproved historical public media: {issue}"
        for issue in find_unapproved_historical_public_media(
            _historical_public_media(root),
            approved_public_media,
        )
    ]
    findings += [
        f"provenance: {error}"
        for error in provenance.verify_manifest(images, manifest)
    ]
    sources = json.loads(
        (root / "tools" / "public_image_sources.json").read_text(
            encoding="utf-8"
        )
    )
    try:
        expected_manifest = provenance.build_manifest(images, sources)
    except FileNotFoundError as error:
        findings.append(f"provenance: {error}")
    else:
        if manifest != expected_manifest:
            findings.append(
                "provenance: manifest does not match declared image sources"
            )

    fixture_module = _load_module(
        root / "tools" / "validate_public_fixture.py",
        "validate_public_fixture",
    )
    fixture = json.loads(
        (root / "site" / "data" / "sample-journal.json").read_text(
            encoding="utf-8"
        )
    )
    findings += [
        f"fixture: {error}"
        for error in fixture_module.validate_fixture(fixture)
    ]

    if private_terms:
        findings = [
            _redact_private_terms(finding, private_terms) for finding in findings
        ]
    for finding in findings:
        print(finding)
    if not findings:
        print("public readiness audit: clean")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
