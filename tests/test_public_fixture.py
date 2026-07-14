import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "site" / "data" / "sample-journal.json"


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_public_fixture", ROOT / "tools" / "validate_public_fixture.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_committed_fixture_is_valid_and_synthetic():
    data = _fixture()
    assert data["meta"]["synthetic"] is True
    assert validator.validate_fixture(data) == []


def test_missing_synthetic_flag_is_rejected():
    data = _fixture()
    data["meta"]["synthetic"] = False
    assert "meta.synthetic must be true" in validator.validate_fixture(data)


def test_duplicate_entry_ids_are_rejected():
    data = _fixture()
    data["entries"][1]["id"] = data["entries"][0]["id"]
    assert "entry ids must be unique" in validator.validate_fixture(data)


def test_report_evidence_must_reference_existing_entries():
    data = _fixture()
    data["report"]["sections"][0]["evidence"] = ["e-does-not-exist"]
    errors = validator.validate_fixture(data)
    assert any("unknown entry id" in error for error in errors)


def test_entries_must_span_three_distinct_years():
    data = _fixture()
    for index, entry in enumerate(data["entries"]):
        entry["date"] = f"2024-01-0{(index % 9) + 1}"
    assert "entries must span at least three distinct years" in validator.validate_fixture(data)


def test_conversation_requires_user_and_assistant():
    data = _fixture()
    for message in data["conversation"]["messages"]:
        message["role"] = "user"
    assert "conversation must include both a user and an assistant message" in validator.validate_fixture(data)


def test_conversation_rejects_unknown_roles():
    data = _fixture()
    data["conversation"]["messages"][0]["role"] = "system"
    assert any(
        "role must be user or assistant" in error
        for error in validator.validate_fixture(data)
    )


def test_public_fixture_is_not_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "site/data/sample-journal.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    # git check-ignore exits 0 when a path IS ignored, 1 when it is not.
    assert result.returncode == 1, (
        "site/data/sample-journal.json must not be gitignored so it can be added "
        f"without `git add -f` (check-ignore matched: {result.stdout.strip()!r}, "
        f"stderr: {result.stderr.strip()!r})"
    )
