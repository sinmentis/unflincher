"""Unit tests for unflincher.request_envelope: the one exact prepared-request object shared by
context-budget preflight and real SDK generation (see llm.stream_completion)."""
from unflincher.request_envelope import (
    ASSEMBLY_VERSION,
    build_envelope,
    canonical_json,
    fingerprint,
    matches_stored_assembly,
)


def test_build_envelope_carries_sdk_visible_fields():
    envelope = build_envelope(
        "系统提示", "用户内容", "claude-sonnet-4.6", target_kind="entry_commentary", target_id=12
    )
    assert envelope.system_mode == "replace"
    assert envelope.system_content == "系统提示"
    assert envelope.user_content == "用户内容"
    assert envelope.model == "claude-sonnet-4.6"
    assert envelope.available_tools == ()
    assert envelope.working_directory == "/tmp"
    assert envelope.skip_custom_instructions is True
    assert envelope.enable_config_discovery is False
    assert envelope.enable_skills is False
    assert envelope.streaming is True
    assert envelope.assembly_version == ASSEMBLY_VERSION
    assert envelope.target_kind == "entry_commentary"
    assert envelope.target_id == "12"  # stringified for stable fingerprinting


def test_build_envelope_target_id_none_by_default():
    envelope = build_envelope("s", "u", "m", target_kind="aggregate_report")
    assert envelope.target_id is None


def test_envelope_is_frozen_and_immutable():
    envelope = build_envelope("s", "u", "m", target_kind="aggregate_report")
    try:
        envelope.system_content = "changed"
    except Exception as exc:
        assert "frozen" in str(exc) or "can't set attribute" in str(exc) or True
    else:
        raise AssertionError("expected frozen dataclass to reject attribute assignment")


def test_canonical_json_is_deterministic_across_calls():
    e1 = build_envelope("同样的系统提示", "同样的用户内容", "test-model", target_kind="entry_commentary", target_id=5)
    e2 = build_envelope("同样的系统提示", "同样的用户内容", "test-model", target_kind="entry_commentary", target_id=5)
    assert canonical_json(e1) == canonical_json(e2)


def test_canonical_json_differs_when_any_field_differs():
    base = build_envelope("系统", "用户", "test-model", target_kind="entry_commentary", target_id=1)
    different_system = build_envelope("系统2", "用户", "test-model", target_kind="entry_commentary", target_id=1)
    different_user = build_envelope("系统", "用户2", "test-model", target_kind="entry_commentary", target_id=1)
    different_model = build_envelope("系统", "用户", "other-model", target_kind="entry_commentary", target_id=1)
    different_target = build_envelope("系统", "用户", "test-model", target_kind="entry_commentary", target_id=2)
    different_kind = build_envelope("系统", "用户", "test-model", target_kind="aggregate_report", target_id=1)

    others = [different_system, different_user, different_model, different_target, different_kind]
    base_json = canonical_json(base)
    for other in others:
        assert canonical_json(other) != base_json


def test_fingerprint_is_stable_sha256_hex():
    envelope = build_envelope("系统", "用户", "test-model", target_kind="entry_commentary", target_id=1)
    fp1 = fingerprint(envelope)
    fp2 = fingerprint(envelope)
    assert fp1 == fp2
    assert len(fp1) == 64
    assert all(c in "0123456789abcdef" for c in fp1)


def test_fingerprint_changes_when_content_changes():
    e1 = build_envelope("系统", "用户A", "test-model", target_kind="entry_commentary", target_id=1)
    e2 = build_envelope("系统", "用户B", "test-model", target_kind="entry_commentary", target_id=1)
    assert fingerprint(e1) != fingerprint(e2)


def test_fingerprint_changes_when_assembly_version_changes(monkeypatch):
    import unflincher.request_envelope as module

    e1 = build_envelope("系统", "用户", "test-model", target_kind="entry_commentary", target_id=1)
    fp1 = fingerprint(e1)

    monkeypatch.setattr(module, "ASSEMBLY_VERSION", module.ASSEMBLY_VERSION + 1)
    e2 = build_envelope("系统", "用户", "test-model", target_kind="entry_commentary", target_id=1)
    fp2 = fingerprint(e2)

    assert fp1 != fp2


def test_matches_stored_assembly_true_for_identical_reconstruction():
    envelope = build_envelope("系统", "用户", "test-model", target_kind="entry_commentary", target_id=1)
    stored_fp = fingerprint(envelope)

    rebuilt = build_envelope("系统", "用户", "test-model", target_kind="entry_commentary", target_id=1)
    assert matches_stored_assembly(
        rebuilt, stored_assembly_version=ASSEMBLY_VERSION, stored_fingerprint=stored_fp
    )


def test_matches_stored_assembly_false_when_fingerprint_differs():
    envelope = build_envelope("系统", "用户A", "test-model", target_kind="entry_commentary", target_id=1)
    stored_fp = fingerprint(envelope)

    rebuilt = build_envelope("系统", "用户B", "test-model", target_kind="entry_commentary", target_id=1)
    assert not matches_stored_assembly(
        rebuilt, stored_assembly_version=ASSEMBLY_VERSION, stored_fingerprint=stored_fp
    )


def test_matches_stored_assembly_false_when_assembly_version_differs():
    envelope = build_envelope("系统", "用户", "test-model", target_kind="entry_commentary", target_id=1)
    stored_fp = fingerprint(envelope)
    assert not matches_stored_assembly(
        envelope, stored_assembly_version=ASSEMBLY_VERSION + 1, stored_fingerprint=stored_fp
    )
