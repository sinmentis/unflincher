"""Tests for unflincher.regen_enqueue: prepare+preflight every concrete request a job would
generate, then atomically enqueue it, rebuilding once (bounded) if the archive changes between
preflight and the atomic enqueue transaction. Also covers retry_job_item_with_admission, the deep
retry-admission interface that must refuse to run (writing nothing) on a format/fingerprint
mismatch or a capacity failure, before ever reaching the atomic DB retry helper."""
import pytest

from unflincher.context_budget import ContextTooLargeError, ModelLimitsUnavailableError
from unflincher.db import (
    ItemJobMismatchError,
    RequestFormatChangedError,
    StaleOrSupersededRetryError,
    entry_target_key,
    fail_job_item,
    get_active_prompt,
    get_connection,
    get_job_entry_snapshot,
    get_lease_by_target,
    init_schema,
    migrate_generation_safety,
    migrate_persona_prompt_model,
    release_lease,
    report_target_key,
    set_active_prompt,
)
from unflincher.regen_enqueue import (
    enqueue_apply_all_job,
    enqueue_single_entry_job,
    retry_job_item_with_admission,
)

import unflincher.llm as llm_module


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.db"))
    init_schema(c)
    migrate_persona_prompt_model(c)
    migrate_generation_safety(c)
    yield c
    c.close()


class _FakeLimits:
    def __init__(self, max_prompt_tokens):
        self.max_prompt_tokens = max_prompt_tokens


class _FakeCapabilities:
    def __init__(self, max_prompt_tokens):
        self.limits = _FakeLimits(max_prompt_tokens)


class _FakeModelInfo:
    def __init__(self, id, max_prompt_tokens):
        self.id = id
        self.name = id
        self.capabilities = _FakeCapabilities(max_prompt_tokens)


class _FakeCopilotClient:
    def __init__(self, max_prompt_tokens=200_000):
        self.max_prompt_tokens = max_prompt_tokens
        self.list_models_calls = 0

    async def start(self):
        pass

    async def stop(self):
        pass

    async def force_stop(self):
        pass

    async def list_models(self):
        self.list_models_calls += 1
        return [_FakeModelInfo("test-model", self.max_prompt_tokens)]


@pytest.fixture(autouse=True)
def _reset_llm_state(monkeypatch):
    import asyncio
    monkeypatch.setattr(llm_module, "_client", None)
    monkeypatch.setattr(llm_module, "_client_generation", 0)
    monkeypatch.setattr(llm_module, "_active_count", 0)
    monkeypatch.setattr(llm_module, "_refresh_active", False)
    monkeypatch.setattr(llm_module, "_lifecycle_cond", asyncio.Condition())
    monkeypatch.setattr(llm_module, "_llm_semaphore", asyncio.Semaphore(4))
    yield


def _seed_entry(conn, title="e", entry_date="2026-01-01"):
    return conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', ?, 'import')",
        (title, entry_date),
    ).lastrowid


# ---------------------------------------------------------------------------
# enqueue_single_entry_job
# ---------------------------------------------------------------------------

async def test_enqueue_single_entry_job_happy_path(conn, monkeypatch):
    fake = _FakeCopilotClient()
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)

    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "人设", "test-model")

    job_id = await enqueue_single_entry_job(
        conn, entry_id=e1, prompt_version_id=prompt_id, persona_text="人设", model="test-model",
        owner_token="owner-a",
    )

    job = conn.execute("SELECT * FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "running"
    assert job["snapshot_entry_count"] == 1
    assert get_job_entry_snapshot(conn, job_id) == [e1]
    item = conn.execute("SELECT * FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchone()
    assert item["target_type"] == "entry_commentary"
    assert item["entry_id"] == e1
    assert item["request_format_version"] >= 1
    assert item["request_fingerprint"]
    assert get_lease_by_target(conn, entry_target_key(e1)) is not None


async def test_enqueue_single_entry_job_raises_context_too_large_before_any_write(conn, monkeypatch):
    fake = _FakeCopilotClient(max_prompt_tokens=1)
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)

    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "人设" * 500, "test-model")

    with pytest.raises(ContextTooLargeError):
        await enqueue_single_entry_job(
            conn, entry_id=e1, prompt_version_id=prompt_id, persona_text="人设" * 500,
            model="test-model", owner_token="owner-a",
        )

    assert conn.execute("SELECT COUNT(*) AS n FROM regen_job").fetchone()["n"] == 0
    assert get_lease_by_target(conn, entry_target_key(e1)) is None


async def test_enqueue_single_entry_job_rebuilds_once_when_archive_changes(conn, monkeypatch):
    fake = _FakeCopilotClient()
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)

    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "人设", "test-model")

    import unflincher.regen_enqueue as regen_enqueue_module
    real_get_ordered = regen_enqueue_module.get_ordered_entry_ids
    call_count = {"n": 0}

    def _flaky_get_ordered(c):
        call_count["n"] += 1
        ids = real_get_ordered(c)
        if call_count["n"] == 1:
            # Simulate a concurrent write landing AFTER this preflight snapshot was captured but
            # before the atomic enqueue commits: insert a new entry now so the SECOND read (inside
            # enqueue_snapshot_regen_job's own transaction) sees something different.
            _seed_entry(c, "late-arrival", "2026-06-01")
        return ids

    monkeypatch.setattr(regen_enqueue_module, "get_ordered_entry_ids", _flaky_get_ordered)

    job_id = await enqueue_single_entry_job(
        conn, entry_id=e1, prompt_version_id=prompt_id, persona_text="人设", model="test-model",
        owner_token="owner-a",
    )

    # Succeeded on the rebuilt attempt, and its snapshot reflects the FULL archive as of that
    # later, successful attempt (both entries), not the stale first snapshot.
    job = conn.execute("SELECT snapshot_entry_count FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["snapshot_entry_count"] == 2
    assert call_count["n"] >= 2


async def test_enqueue_single_entry_job_raises_value_error_for_unknown_entry(conn, monkeypatch):
    fake = _FakeCopilotClient()
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)
    prompt_id = set_active_prompt(conn, "人设", "test-model")

    with pytest.raises(ValueError):
        await enqueue_single_entry_job(
            conn, entry_id=999, prompt_version_id=prompt_id, persona_text="人设", model="test-model",
            owner_token="owner-a",
        )


# ---------------------------------------------------------------------------
# enqueue_apply_all_job
# ---------------------------------------------------------------------------

async def test_enqueue_apply_all_job_activates_prompt_and_covers_every_entry_plus_report(conn, monkeypatch):
    fake = _FakeCopilotClient()
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)

    e1 = _seed_entry(conn, "e1", "2026-01-01")
    e2 = _seed_entry(conn, "e2", "2026-01-02")
    set_active_prompt(conn, "original", "test-model")

    job_id, activated_prompt_id = await enqueue_apply_all_job(
        conn, persona_text="draft persona", model="test-model", owner_token="owner-a", activate=True,
    )

    assert activated_prompt_id is not None
    active = get_active_prompt(conn)
    assert active["id"] == activated_prompt_id
    assert active["body_text"] == "draft persona"

    items = conn.execute("SELECT * FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchall()
    assert len(items) == 3  # e1, e2, report
    entry_items = [i for i in items if i["target_type"] == "entry_commentary"]
    assert {i["entry_id"] for i in entry_items} == {e1, e2}
    report_items = [i for i in items if i["target_type"] == "aggregate_report"]
    assert len(report_items) == 1
    assert get_lease_by_target(conn, report_target_key()) is not None


async def test_enqueue_apply_all_job_ignores_a_forged_activate_preset_key_hint(conn, monkeypatch):
    """activate_preset_key is a caller-claimed hint only -- exact Analyst body text classifies
    correctly regardless of a forged/mismatched hint."""
    from unflincher.perspectives import get_preset

    fake = _FakeCopilotClient()
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)
    analyst = get_preset("analyst")
    _seed_entry(conn)

    _job_id, activated_prompt_id = await enqueue_apply_all_job(
        conn, persona_text=analyst.prompt, model="test-model", owner_token="owner-a",
        activate=True, activate_preset_key="coach",
    )

    active = get_active_prompt(conn)
    assert active["id"] == activated_prompt_id
    assert active["preset_key"] == "analyst"


async def test_enqueue_apply_all_job_reuses_existing_prompt_when_not_activating(conn, monkeypatch):
    fake = _FakeCopilotClient()
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)

    _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "already active", "test-model")

    job_id, activated_prompt_id = await enqueue_apply_all_job(
        conn, persona_text="already active", model="test-model", owner_token="owner-a",
        activate=False, prompt_version_id=prompt_id,
    )

    assert activated_prompt_id is None
    job = conn.execute("SELECT prompt_version_id FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["prompt_version_id"] == prompt_id
    # Only one prompt row exists -- reusing, not creating a new version.
    assert conn.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"] == 1


async def test_enqueue_apply_all_job_raises_context_too_large_for_first_offending_entry_before_any_write(conn, monkeypatch):
    fake = _FakeCopilotClient(max_prompt_tokens=1)
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)

    _seed_entry(conn, "e1", "2026-01-01")
    _seed_entry(conn, "e2", "2026-01-02")

    with pytest.raises(ContextTooLargeError):
        await enqueue_apply_all_job(
            conn, persona_text="人设" * 500, model="test-model", owner_token="owner-a", activate=True,
        )

    assert conn.execute("SELECT COUNT(*) AS n FROM regen_job").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"] == 0


async def test_enqueue_apply_all_job_validates_activate_and_prompt_version_id_combo(conn, monkeypatch):
    fake = _FakeCopilotClient()
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)
    prompt_id = set_active_prompt(conn, "p", "test-model")

    with pytest.raises(ValueError):
        await enqueue_apply_all_job(
            conn, persona_text="p", model="test-model", owner_token="o",
            activate=True, prompt_version_id=prompt_id,
        )
    with pytest.raises(ValueError):
        await enqueue_apply_all_job(
            conn, persona_text="p", model="test-model", owner_token="o", activate=False,
        )


async def test_enqueue_apply_all_job_fetches_model_limit_once_for_the_whole_batch(conn, monkeypatch):
    fake = _FakeCopilotClient()
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)

    for i in range(5):
        _seed_entry(conn, f"e{i}", f"2026-01-{i + 1:02d}")

    await enqueue_apply_all_job(
        conn, persona_text="人设", model="test-model", owner_token="owner-a", activate=True,
    )

    # One model-list fetch for the whole apply-all batch (5 entries + report), not one per item.
    assert fake.list_models_calls == 1


# ---------------------------------------------------------------------------
# retry_job_item_with_admission
# ---------------------------------------------------------------------------

async def _enqueue_and_fail_single_item(conn, monkeypatch, *, persona_text="人设", model="test-model"):
    """Enqueue a real single-entry job via enqueue_single_entry_job (so the item's stored
    fingerprint is exactly what the worker would reconstruct), then simulate the job having
    completed with that one item failed -- release its admission-time lease (as the worker would
    on failure), mark it 'failed', and mark the job 'done'."""
    fake = _FakeCopilotClient()
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)
    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, persona_text, model)
    job_id = await enqueue_single_entry_job(
        conn, entry_id=e1, prompt_version_id=prompt_id, persona_text=persona_text, model=model,
        owner_token="worker",
    )
    item = conn.execute("SELECT * FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchone()
    release_lease(conn, get_lease_by_target(conn, entry_target_key(e1))["id"])
    fail_job_item(conn, item["id"], "boom")
    conn.execute("UPDATE regen_job SET status = 'done' WHERE id = ?", (job_id,))
    return job_id, item["id"], e1, prompt_id


async def test_retry_job_item_with_admission_happy_path(conn, monkeypatch):
    job_id, item_id, e1, _ = await _enqueue_and_fail_single_item(conn, monkeypatch)

    returned_job_id = await retry_job_item_with_admission(
        conn, item_id=item_id, owner_token="retry-owner",
    )

    assert returned_job_id == job_id
    item = conn.execute("SELECT * FROM regen_job_item WHERE id = ?", (item_id,)).fetchone()
    assert item["status"] == "pending"
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "running"
    lease = get_lease_by_target(conn, entry_target_key(e1))
    assert lease is not None
    assert lease["owner_token"] == "retry-owner"


async def test_retry_job_item_with_admission_returns_authoritative_job_id_and_rejects_mismatch(conn, monkeypatch):
    job_id, item_id, _, _ = await _enqueue_and_fail_single_item(conn, monkeypatch)

    with pytest.raises(ItemJobMismatchError):
        await retry_job_item_with_admission(
            conn, item_id=item_id, owner_token="retry-owner", expected_job_id=job_id + 999,
        )
    # No write on mismatch.
    item = conn.execute("SELECT status FROM regen_job_item WHERE id = ?", (item_id,)).fetchone()
    assert item["status"] == "failed"


async def test_retry_job_item_with_admission_rejects_fingerprint_mismatch_before_any_write(conn, monkeypatch):
    job_id, item_id, e1, _ = await _enqueue_and_fail_single_item(conn, monkeypatch)
    # Corrupt the stored fingerprint as if the code that assembles requests changed since
    # admission.
    conn.execute(
        "UPDATE regen_job_item SET request_fingerprint = 'stale-fingerprint' WHERE id = ?",
        (item_id,),
    )

    with pytest.raises(RequestFormatChangedError):
        await retry_job_item_with_admission(conn, item_id=item_id, owner_token="retry-owner")

    item = conn.execute("SELECT status FROM regen_job_item WHERE id = ?", (item_id,)).fetchone()
    assert item["status"] == "failed"  # never requeued
    assert get_lease_by_target(conn, entry_target_key(e1)) is None  # no lease acquired


async def test_retry_job_item_with_admission_rejects_context_too_large_before_any_write(conn, monkeypatch):
    job_id, item_id, e1, _ = await _enqueue_and_fail_single_item(conn, monkeypatch)

    async def _tiny_limit(model):
        return 1
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _tiny_limit)

    with pytest.raises(ContextTooLargeError):
        await retry_job_item_with_admission(conn, item_id=item_id, owner_token="retry-owner")

    item = conn.execute("SELECT status FROM regen_job_item WHERE id = ?", (item_id,)).fetchone()
    assert item["status"] == "failed"
    assert get_lease_by_target(conn, entry_target_key(e1)) is None


async def test_retry_job_item_with_admission_rejects_model_limits_unavailable(conn, monkeypatch):
    job_id, item_id, e1, _ = await _enqueue_and_fail_single_item(conn, monkeypatch)

    async def _raise(model):
        raise ModelLimitsUnavailableError(model, "boom")
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _raise)

    with pytest.raises(ModelLimitsUnavailableError):
        await retry_job_item_with_admission(conn, item_id=item_id, owner_token="retry-owner")

    item = conn.execute("SELECT status FROM regen_job_item WHERE id = ?", (item_id,)).fetchone()
    assert item["status"] == "failed"
    assert get_lease_by_target(conn, entry_target_key(e1)) is None


async def test_retry_job_item_with_admission_rejects_when_job_not_done(conn, monkeypatch):
    job_id, item_id, _, _ = await _enqueue_and_fail_single_item(conn, monkeypatch)
    conn.execute("UPDATE regen_job SET status = 'running' WHERE id = ?", (job_id,))

    with pytest.raises(StaleOrSupersededRetryError):
        await retry_job_item_with_admission(conn, item_id=item_id, owner_token="retry-owner")


async def test_retry_job_item_with_admission_rejects_snapshot_count_mismatch(conn, monkeypatch):
    job_id, item_id, _, _ = await _enqueue_and_fail_single_item(conn, monkeypatch)
    conn.execute("DELETE FROM regen_job_entry_snapshot WHERE job_id = ?", (job_id,))

    with pytest.raises(StaleOrSupersededRetryError):
        await retry_job_item_with_admission(conn, item_id=item_id, owner_token="retry-owner")


async def test_retry_job_item_with_admission_rejects_missing_prompt(conn, monkeypatch):
    job_id, item_id, _, prompt_id = await _enqueue_and_fail_single_item(conn, monkeypatch)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM persona_prompt WHERE id = ?", (prompt_id,))
    conn.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(StaleOrSupersededRetryError):
        await retry_job_item_with_admission(conn, item_id=item_id, owner_token="retry-owner")


async def test_retry_job_item_with_admission_rejects_missing_snapshotted_entry(conn, monkeypatch):
    job_id, item_id, e1, _ = await _enqueue_and_fail_single_item(conn, monkeypatch)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM diary_entry WHERE id = ?", (e1,))
    conn.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(StaleOrSupersededRetryError):
        await retry_job_item_with_admission(conn, item_id=item_id, owner_token="retry-owner")
