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


def test_entries_use_reflection_not_commentary_field():
    data = _fixture()
    for entry in data["entries"]:
        assert "reflection" in entry
        assert "commentary" not in entry


def test_workshop_has_exactly_five_perspectives_in_canonical_order():
    data = _fixture()
    keys = [p["key"] for p in data["workshop"]["perspectives"]]
    assert keys == ["companion", "coach", "challenger", "analyst", "custom"]


def test_workshop_preset_instructions_match_the_canonical_perspective_catalog():
    from unflincher.perspectives import PERSPECTIVE_KEYS, get_preset

    data = _fixture()
    by_key = {p["key"]: p for p in data["workshop"]["perspectives"]}
    for key in PERSPECTIVE_KEYS:
        assert by_key[key]["instructions"] == get_preset(key).prompt


def test_workshop_preset_names_match_canonical_i18n_names():
    from unflincher.i18n import t
    from unflincher.perspectives import PERSPECTIVE_KEYS

    data = _fixture()
    by_key = {p["key"]: p for p in data["workshop"]["perspectives"]}
    for key in PERSPECTIVE_KEYS:
        assert by_key[key]["name"] == t("en", f"perspective.{key}.name")


def test_workshop_custom_does_not_impersonate_a_shipped_preset():
    from unflincher.i18n import t
    from unflincher.perspectives import PERSPECTIVE_KEYS

    data = _fixture()
    custom = data["workshop"]["perspectives"][-1]
    assert custom["key"] == "custom"
    preset_names = {t("en", f"perspective.{key}.name") for key in PERSPECTIVE_KEYS}
    assert custom["name"] not in preset_names


def test_workshop_rejects_wrong_number_or_order_of_perspectives():
    data = _fixture()
    data["workshop"]["perspectives"] = data["workshop"]["perspectives"][:4]
    errors = validator.validate_fixture(data)
    assert any("workshop.perspectives must be a list of exactly 5 items" in e for e in errors)

    data = _fixture()
    data["workshop"]["perspectives"][0]["key"] = "analyst"
    errors = validator.validate_fixture(data)
    assert any("workshop.perspectives[0].key must be 'companion'" in e for e in errors)


def test_workshop_rejects_drifted_preset_instructions():
    data = _fixture()
    data["workshop"]["perspectives"][0]["instructions"] = "Something else entirely."
    errors = validator.validate_fixture(data)
    assert any(
        "instructions must match the canonical Perspective prompt text exactly" in e
        for e in errors
    )


def test_bare_python_validator_still_enforces_canonical_preset_text(tmp_path):
    data = _fixture()
    data["workshop"]["perspectives"][0]["instructions"] = "Something else entirely."
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(data), encoding="utf-8")

    result = subprocess.run(
        ["python3", "tools/validate_public_fixture.py", str(fixture_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "instructions must match the canonical Perspective prompt text exactly" in result.stdout


def test_workshop_rejects_drifted_preset_name():
    data = _fixture()
    data["workshop"]["perspectives"][0]["name"] = "Friend"
    errors = validator.validate_fixture(data)
    assert any("name must match the canonical Perspective name" in e for e in errors)


def test_workshop_rejects_custom_impersonating_a_preset():
    data = _fixture()
    data["workshop"]["perspectives"][-1]["name"] = "Analyst"
    errors = validator.validate_fixture(data)
    assert any("must not impersonate a shipped Perspective preset" in e for e in errors)

    data = _fixture()
    data["workshop"]["perspectives"][-1]["instructions"] = data["workshop"]["perspectives"][0][
        "instructions"
    ]
    errors = validator.validate_fixture(data)
    assert any("must not exactly match a shipped Perspective preset" in e for e in errors)


def test_workshop_requires_entry_id_referencing_an_existing_entry():
    data = _fixture()
    data["workshop"]["entry_id"] = "e-does-not-exist"
    errors = validator.validate_fixture(data)
    assert "workshop.entry_id must reference an existing entry id" in errors


def test_workshop_readings_must_reference_the_shared_entry_date():
    data = _fixture()
    data["workshop"]["perspectives"][0]["reading"] = "A reading with no dated evidence at all."
    errors = validator.validate_fixture(data)
    assert any("must reference the dated entry evidence" in e for e in errors)


def test_workshop_readings_reject_dates_absent_from_the_fixture():
    data = _fixture()
    data["workshop"]["perspectives"][0]["reading"] += " A different pattern appeared on 1999-01-01."
    errors = validator.validate_fixture(data)
    assert any("references unknown entry date '1999-01-01'" in e for e in errors)


def test_workshop_readings_must_be_recognizably_distinct():
    data = _fixture()
    shared_text = data["workshop"]["perspectives"][0]["reading"]
    data["workshop"]["perspectives"][1]["reading"] = shared_text
    errors = validator.validate_fixture(data)
    assert any("must be recognizably distinct across Perspectives" in e for e in errors)


def test_workshop_readings_reject_near_duplicate_stances():
    data = _fixture()
    first = data["workshop"]["perspectives"][0]["reading"]
    data["workshop"]["perspectives"][1]["reading"] = first.replace("real", "genuine", 1)
    errors = validator.validate_fixture(data)
    assert any("must be recognizably distinct across Perspectives" in e for e in errors)


def test_rejects_diagnosis_phrases():
    data = _fixture()
    data["entries"][0]["reflection"] = "You have clinical depression and need treatment."
    errors = validator.validate_fixture(data)
    assert any("disallowed diagnosis phrase" in e for e in errors)


def test_rejects_clinical_role_phrases():
    data = _fixture()
    data["entries"][0]["reflection"] = "Speaking as your therapist, here is my read."
    errors = validator.validate_fixture(data)
    assert any("disallowed clinical-role phrase" in e for e in errors)


def test_rejects_treatment_phrases():
    data = _fixture()
    data["entries"][0]["reflection"] = "Here is your treatment plan going forward."
    errors = validator.validate_fixture(data)
    assert any("disallowed treatment phrase" in e for e in errors)


def test_rejects_humiliation_phrases():
    data = _fixture()
    data["entries"][0]["reflection"] = "You are pathetic for waiting this long."
    errors = validator.validate_fixture(data)
    assert any("disallowed humiliation phrase" in e for e in errors)


def test_rejects_false_intimacy_phrases():
    data = _fixture()
    data["entries"][0]["reflection"] = "I love you and I will never leave you."
    errors = validator.validate_fixture(data)
    assert any("disallowed false-intimacy phrase" in e for e in errors)


def test_unsafe_phrase_scan_covers_report_and_conversation_and_workshop_text():
    data = _fixture()
    data["report"]["sections"][0]["body"] = "As your doctor, I diagnose this pattern."
    data["conversation"]["messages"][0]["text"] = "I will always be here for you no matter what."
    data["workshop"]["perspectives"][-1]["reading"] = "You are worthless for waiting this long."
    errors = validator.validate_fixture(data)
    assert any(e.startswith("report.sections[0].body") for e in errors)
    assert any(e.startswith("conversation.messages[0].text") for e in errors)
    assert any(e.startswith("workshop.perspectives[4].reading") for e in errors)
