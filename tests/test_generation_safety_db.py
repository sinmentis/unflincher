"""Tests for the generation-safety database foundations: maintenance gate, exclusive per-target
leases, ordered archive snapshots, atomic snapshot+lease enqueue, atomic failed-item retry, and
snapshot-aware startup recovery. See db.py's module docstring for the invariants under test."""
import sqlite3

import pytest

from unflincher.db import (
    ArchiveChangedError,
    MaintenanceLockedError,
    PreparedRegenTarget,
    RecoveryResult,
    StaleOrSupersededRetryError,
    TargetBusyError,
    acquire_lease,
    clear_stale_leases,
    conversation_thread_key,
    convert_lease_target,
    enqueue_snapshot_regen_job,
    entry_target_key,
    entry_thread_key,
    fail_job_item,
    get_active_prompt,
    get_connection,
    get_entries_in_order,
    get_job_entry_snapshot,
    get_lease_by_target,
    get_maintenance_locked,
    get_ordered_entry_ids,
    init_schema,
    migrate_generation_safety,
    migrate_persona_prompt_model,
    recover_or_cancel_running_jobs,
    release_lease,
    report_target_key,
    retry_failed_job_item,
    set_active_prompt,
    set_maintenance_locked,
)


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    c = get_connection(db_path)
    init_schema(c)
    migrate_persona_prompt_model(c)
    migrate_generation_safety(c)
    yield c
    c.close()


def _seed_entry(conn, title="e", entry_date="2026-01-01"):
    return conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', ?, 'import')",
        (title, entry_date),
    ).lastrowid


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_migrate_generation_safety_adds_columns_and_tables(tmp_path):
    c = get_connection(str(tmp_path / "m.db"))
    init_schema(c)
    job_cols_before = {r["name"] for r in c.execute("PRAGMA table_info(regen_job)")}
    assert "snapshot_entry_count" not in job_cols_before

    migrate_generation_safety(c)

    job_cols = {r["name"] for r in c.execute("PRAGMA table_info(regen_job)")}
    item_cols = {r["name"] for r in c.execute("PRAGMA table_info(regen_job_item)")}
    assert "snapshot_entry_count" in job_cols
    assert {"request_format_version", "request_fingerprint", "baseline_result_id"} <= item_cols

    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"maintenance_control", "generation_lease", "regen_job_entry_snapshot"} <= tables
    c.close()


def test_migrate_generation_safety_is_idempotent(tmp_path):
    c = get_connection(str(tmp_path / "m.db"))
    init_schema(c)
    migrate_generation_safety(c)
    migrate_generation_safety(c)  # second run must not error
    job_cols = [r for r in c.execute("PRAGMA table_info(regen_job)") if r["name"] == "snapshot_entry_count"]
    assert len(job_cols) == 1
    c.close()


def test_pre_existing_rows_get_null_snapshot_and_fingerprint_columns(tmp_path):
    # Simulates a live production DB: a job/item written the OLD way (before these columns
    # existed) must backfill to NULL, the exact signal recovery/retry use to refuse it.
    c = get_connection(str(tmp_path / "old.db"))
    init_schema(c)
    migrate_persona_prompt_model(c)
    prompt_id = set_active_prompt(c, "p", "test-model")
    job_id = c.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'done')", (prompt_id,)
    ).lastrowid
    item_id = c.execute(
        "INSERT INTO regen_job_item (job_id, target_type, status) VALUES (?, 'aggregate_report', 'failed')",
        (job_id,),
    ).lastrowid

    migrate_generation_safety(c)

    job = c.execute("SELECT snapshot_entry_count FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    item = c.execute(
        "SELECT request_format_version, request_fingerprint, baseline_result_id FROM regen_job_item WHERE id = ?",
        (item_id,),
    ).fetchone()
    assert job["snapshot_entry_count"] is None
    assert item["request_format_version"] is None
    assert item["request_fingerprint"] is None
    assert item["baseline_result_id"] is None
    c.close()


# ---------------------------------------------------------------------------
# Maintenance flag
# ---------------------------------------------------------------------------

def test_maintenance_defaults_to_unlocked(conn):
    assert get_maintenance_locked(conn) is False


def test_set_maintenance_locked_round_trips(conn):
    set_maintenance_locked(conn, True)
    assert get_maintenance_locked(conn) is True
    set_maintenance_locked(conn, False)
    assert get_maintenance_locked(conn) is False


# ---------------------------------------------------------------------------
# Target-key helpers
# ---------------------------------------------------------------------------

def test_target_key_helpers_are_stable_and_distinct():
    assert entry_target_key(12) == "entry:12"
    assert report_target_key() == "report"
    assert entry_thread_key(12) == "entry-thread:12"
    assert conversation_thread_key(7) == "conversation:7"
    assert entry_target_key(12) != entry_thread_key(12)


# ---------------------------------------------------------------------------
# Lease primitives
# ---------------------------------------------------------------------------

def test_acquire_lease_succeeds_and_is_readable(conn):
    lease_id = acquire_lease(conn, "entry:1", "direct", "owner-a")
    row = get_lease_by_target(conn, "entry:1")
    assert row["id"] == lease_id
    assert row["lease_kind"] == "direct"
    assert row["owner_token"] == "owner-a"


def test_acquire_lease_rejects_busy_target(conn):
    acquire_lease(conn, "entry:1", "direct", "owner-a")
    with pytest.raises(TargetBusyError):
        acquire_lease(conn, "entry:1", "direct", "owner-b")


def test_acquire_lease_rejects_when_maintenance_locked_and_writes_nothing(conn):
    set_maintenance_locked(conn, True)
    with pytest.raises(MaintenanceLockedError):
        acquire_lease(conn, "entry:1", "direct", "owner-a")
    assert get_lease_by_target(conn, "entry:1") is None


def test_release_lease_frees_the_target(conn):
    lease_id = acquire_lease(conn, "entry:1", "direct", "owner-a")
    release_lease(conn, lease_id)
    assert get_lease_by_target(conn, "entry:1") is None
    # Now a different owner can acquire the same target.
    acquire_lease(conn, "entry:1", "direct", "owner-b")


def test_convert_lease_target_repoints_atomically(conn):
    lease_id = acquire_lease(conn, "request:abc123", "request", "owner-a")
    convert_lease_target(conn, lease_id, "conversation:5")
    assert get_lease_by_target(conn, "request:abc123") is None
    row = get_lease_by_target(conn, "conversation:5")
    assert row["id"] == lease_id


def test_convert_lease_target_rejects_when_new_target_already_busy(conn):
    lease_id = acquire_lease(conn, "request:abc123", "request", "owner-a")
    acquire_lease(conn, "conversation:5", "thread", "owner-b")
    with pytest.raises(TargetBusyError):
        convert_lease_target(conn, lease_id, "conversation:5")
    # Original lease is untouched (rolled back).
    assert get_lease_by_target(conn, "request:abc123")["id"] == lease_id


def test_clear_stale_leases_removes_all_leases(conn):
    acquire_lease(conn, "entry:1", "direct", "owner-a")
    acquire_lease(conn, "report", "background", "owner-b")
    removed = clear_stale_leases(conn)
    assert removed == 2
    assert get_lease_by_target(conn, "entry:1") is None
    assert get_lease_by_target(conn, "report") is None


# ---------------------------------------------------------------------------
# Ordered archive snapshot
# ---------------------------------------------------------------------------

def test_get_ordered_entry_ids_orders_by_date_then_id(conn):
    e_late = _seed_entry(conn, "late", "2026-03-01")
    e_early = _seed_entry(conn, "early", "2026-01-01")
    e_mid_a = _seed_entry(conn, "mid-a", "2026-02-01")
    e_mid_b = _seed_entry(conn, "mid-b", "2026-02-01")  # same date as mid_a, inserted after

    ordered = get_ordered_entry_ids(conn)

    assert ordered == [e_early, e_mid_a, e_mid_b, e_late]


def test_get_ordered_entry_ids_empty_archive(conn):
    assert get_ordered_entry_ids(conn) == []


def test_get_entries_in_order_matches_the_given_id_order_not_in_or_date_order(conn):
    e_late = _seed_entry(conn, "late", "2026-03-01")
    e_early = _seed_entry(conn, "early", "2026-01-01")

    rows = get_entries_in_order(conn, [e_late, e_early])
    assert [r["id"] for r in rows] == [e_late, e_early]

    rows_reversed = get_entries_in_order(conn, [e_early, e_late])
    assert [r["id"] for r in rows_reversed] == [e_early, e_late]


def test_get_entries_in_order_empty_list(conn):
    assert get_entries_in_order(conn, []) == []


def test_get_entries_in_order_skips_ids_that_no_longer_resolve(conn):
    e1 = _seed_entry(conn, "e1", "2026-01-01")
    e2 = _seed_entry(conn, "e2", "2026-01-02")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM diary_entry WHERE id = ?", (e1,))
    conn.execute("PRAGMA foreign_keys=ON")

    rows = get_entries_in_order(conn, [e1, e2])

    # e1 no longer resolves -- silently skipped, not a KeyError -- so callers can detect the
    # mismatch themselves by comparing len(result) against len(requested ids).
    assert [r["id"] for r in rows] == [e2]


# ---------------------------------------------------------------------------
# Atomic snapshot+lease enqueue
# ---------------------------------------------------------------------------

def test_enqueue_snapshot_regen_job_happy_path_writes_job_items_and_snapshot(conn):
    e1 = _seed_entry(conn, "e1", "2026-01-01")
    e2 = _seed_entry(conn, "e2", "2026-01-02")
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    targets = [
        PreparedRegenTarget("entry_commentary", e1, 1, "fp-e1"),
        PreparedRegenTarget("entry_commentary", e2, 1, "fp-e2"),
        PreparedRegenTarget("aggregate_report", None, 1, "fp-report"),
    ]

    job_id, activated_prompt_id = enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1, e2],
        targets=targets, owner_token="owner-a",
    )
    assert activated_prompt_id is None  # prompt_version_id path never activates a new version

    job = conn.execute("SELECT * FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "running"
    assert job["snapshot_entry_count"] == 2
    assert get_job_entry_snapshot(conn, job_id) == [e1, e2]

    items = conn.execute(
        "SELECT * FROM regen_job_item WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()
    assert len(items) == 3
    assert {i["request_fingerprint"] for i in items} == {"fp-e1", "fp-e2", "fp-report"}
    assert all(i["request_format_version"] == 1 for i in items)
    assert all(i["baseline_result_id"] is None for i in items)  # no prior successful results

    # Every target now holds an exclusive lease.
    assert get_lease_by_target(conn, entry_target_key(e1)) is not None
    assert get_lease_by_target(conn, entry_target_key(e2)) is not None
    assert get_lease_by_target(conn, report_target_key()) is not None


def test_enqueue_snapshot_regen_job_captures_current_result_as_baseline(conn):
    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    existing_result_id = conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'test-model', 'old take', 'ok')",
        (e1, prompt_id),
    ).lastrowid

    job_id, _ = enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1],
        targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp")],
        owner_token="owner-a",
    )

    item = conn.execute("SELECT * FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchone()
    assert item["baseline_result_id"] == existing_result_id


def test_enqueue_snapshot_regen_job_rejects_when_maintenance_locked(conn):
    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    set_maintenance_locked(conn, True)

    with pytest.raises(MaintenanceLockedError):
        enqueue_snapshot_regen_job(
            conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1],
            targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp")],
            owner_token="owner-a",
        )
    assert conn.execute("SELECT COUNT(*) AS n FROM regen_job").fetchone()["n"] == 0


def test_enqueue_snapshot_regen_job_rejects_archive_changed_between_preflight_and_enqueue(conn):
    e1 = _seed_entry(conn, "e1", "2026-01-01")
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    stale_preflight_ids = [e1]
    # An entry is written AFTER preflight captured its snapshot but BEFORE enqueue commits.
    _seed_entry(conn, "e2-inserted-after-preflight", "2026-01-02")

    with pytest.raises(ArchiveChangedError):
        enqueue_snapshot_regen_job(
            conn, prompt_version_id=prompt_id, preflight_entry_ids=stale_preflight_ids,
            targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp")],
            owner_token="owner-a",
        )
    assert conn.execute("SELECT COUNT(*) AS n FROM regen_job").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM regen_job_entry_snapshot").fetchone()["n"] == 0


def test_enqueue_snapshot_regen_job_rejects_and_rolls_back_when_any_target_busy(conn):
    e1 = _seed_entry(conn, "e1", "2026-01-01")
    e2 = _seed_entry(conn, "e2", "2026-01-02")
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    # Someone else already holds a lease on e2's target.
    acquire_lease(conn, entry_target_key(e2), "direct", "someone-else")

    with pytest.raises(TargetBusyError):
        enqueue_snapshot_regen_job(
            conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1, e2],
            targets=[
                PreparedRegenTarget("entry_commentary", e1, 1, "fp-e1"),
                PreparedRegenTarget("entry_commentary", e2, 1, "fp-e2"),
            ],
            owner_token="owner-a",
        )

    # No job/items/snapshot written, and e1's lease (acquired earlier in the SAME failed
    # transaction) was rolled back too -- not left dangling.
    assert conn.execute("SELECT COUNT(*) AS n FROM regen_job").fetchone()["n"] == 0
    assert get_lease_by_target(conn, entry_target_key(e1)) is None
    # e2's PRE-EXISTING lease (from someone-else) is untouched.
    assert get_lease_by_target(conn, entry_target_key(e2))["owner_token"] == "someone-else"


def test_enqueue_snapshot_regen_job_still_enforces_single_running_job(conn):
    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1],
        targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp1")],
        owner_token="owner-a",
    )
    e2 = _seed_entry(conn, "e2", "2026-02-01")
    with pytest.raises(sqlite3.IntegrityError):
        enqueue_snapshot_regen_job(
            conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1, e2],
            targets=[PreparedRegenTarget("aggregate_report", None, 1, "fp2")],
            owner_token="owner-a",
        )


def test_enqueue_snapshot_regen_job_requires_exactly_one_of_prompt_version_id_or_activate(conn):
    e1 = _seed_entry(conn)
    with pytest.raises(ValueError):
        enqueue_snapshot_regen_job(
            conn, preflight_entry_ids=[e1],
            targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp")],
            owner_token="owner-a",
        )
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    with pytest.raises(ValueError):
        enqueue_snapshot_regen_job(
            conn, prompt_version_id=prompt_id, activate_prompt=("draft", "test-model"),
            preflight_entry_ids=[e1],
            targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp")],
            owner_token="owner-a",
        )


def test_enqueue_snapshot_regen_job_activates_new_prompt_atomically_with_job(conn):
    e1 = _seed_entry(conn)
    original_id = set_active_prompt(conn, "original persona", "test-model")

    job_id, activated_prompt_id = enqueue_snapshot_regen_job(
        conn, activate_prompt=("draft persona", "claude-opus-4.8"),
        preflight_entry_ids=[e1],
        targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp")],
        owner_token="owner-a",
    )

    assert activated_prompt_id is not None
    assert activated_prompt_id != original_id
    active = get_active_prompt(conn)
    assert active["id"] == activated_prompt_id
    assert active["body_text"] == "draft persona"
    assert active["model"] == "claude-opus-4.8"
    job = conn.execute("SELECT prompt_version_id FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["prompt_version_id"] == activated_prompt_id


def test_enqueue_snapshot_regen_job_rolls_back_prompt_activation_when_target_busy(conn):
    e1 = _seed_entry(conn)
    original_id = set_active_prompt(conn, "original persona", "test-model")
    acquire_lease(conn, entry_target_key(e1), "direct", "someone-else")

    with pytest.raises(TargetBusyError):
        enqueue_snapshot_regen_job(
            conn, activate_prompt=("draft persona", "test-model"),
            preflight_entry_ids=[e1],
            targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp")],
            owner_token="owner-a",
        )

    active = get_active_prompt(conn)
    assert active["id"] == original_id  # activation rolled back along with the lease attempt
    assert conn.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# PreparedRegenTarget strict validation
# ---------------------------------------------------------------------------

def test_prepared_regen_target_rejects_entry_commentary_without_entry_id():
    with pytest.raises(ValueError):
        PreparedRegenTarget("entry_commentary", None, 1, "fp")


def test_prepared_regen_target_rejects_aggregate_report_with_entry_id():
    with pytest.raises(ValueError):
        PreparedRegenTarget("aggregate_report", 5, 1, "fp")


def test_prepared_regen_target_rejects_non_positive_format_version():
    with pytest.raises(ValueError):
        PreparedRegenTarget("aggregate_report", None, 0, "fp")
    with pytest.raises(ValueError):
        PreparedRegenTarget("aggregate_report", None, -1, "fp")


def test_prepared_regen_target_rejects_empty_fingerprint():
    with pytest.raises(ValueError):
        PreparedRegenTarget("aggregate_report", None, 1, "")


def test_prepared_regen_target_rejects_invalid_target_type():
    with pytest.raises(ValueError):
        PreparedRegenTarget("something_else", None, 1, "fp")


# ---------------------------------------------------------------------------
# Atomic failed-item retry
# ---------------------------------------------------------------------------

def _enqueue_single_item_job(conn, prompt_id, entry_id, *, fingerprint="fp", version=1):
    job_id, _ = enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=[entry_id],
        targets=[PreparedRegenTarget("entry_commentary", entry_id, version, fingerprint)],
        owner_token="worker",
    )
    return job_id


def test_retry_failed_job_item_happy_path(conn):
    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    job_id = _enqueue_single_item_job(conn, prompt_id, e1)
    item_id = conn.execute("SELECT id FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchone()["id"]
    # Release the admission-time lease (as the worker would once it starts the item), fail it,
    # and mark the job done -- simulating a completed job with one failed item.
    release_lease(conn, get_lease_by_target(conn, entry_target_key(e1))["id"])
    fail_job_item(conn, item_id, "boom")
    conn.execute("UPDATE regen_job SET status = 'done' WHERE id = ?", (job_id,))

    retry_failed_job_item(conn, item_id=item_id, owner_token="retry-owner")

    item = conn.execute("SELECT * FROM regen_job_item WHERE id = ?", (item_id,)).fetchone()
    job = conn.execute("SELECT * FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert item["status"] == "pending"
    assert item["error"] is None
    assert job["status"] == "running"
    lease = get_lease_by_target(conn, entry_target_key(e1))
    assert lease is not None
    assert lease["owner_token"] == "retry-owner"


def test_retry_failed_job_item_rejects_when_maintenance_locked(conn):
    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    job_id = _enqueue_single_item_job(conn, prompt_id, e1)
    item_id = conn.execute("SELECT id FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchone()["id"]
    release_lease(conn, get_lease_by_target(conn, entry_target_key(e1))["id"])
    fail_job_item(conn, item_id, "boom")
    conn.execute("UPDATE regen_job SET status = 'done' WHERE id = ?", (job_id,))
    set_maintenance_locked(conn, True)

    with pytest.raises(MaintenanceLockedError):
        retry_failed_job_item(conn, item_id=item_id, owner_token="retry-owner")
    assert conn.execute("SELECT status FROM regen_job_item WHERE id = ?", (item_id,)).fetchone()["status"] == "failed"


def test_retry_failed_job_item_rejects_legacy_job_without_snapshot(conn):
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    e1 = _seed_entry(conn)
    job_id = conn.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'done')", (prompt_id,)
    ).lastrowid  # no snapshot_entry_count -> legacy
    item_id = conn.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'failed')",
        (job_id, e1),
    ).lastrowid

    with pytest.raises(StaleOrSupersededRetryError):
        retry_failed_job_item(conn, item_id=item_id, owner_token="retry-owner")


def test_retry_failed_job_item_rejects_missing_fingerprint(conn):
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    e1 = _seed_entry(conn)
    job_id = conn.execute(
        "INSERT INTO regen_job (prompt_version_id, status, snapshot_entry_count) VALUES (?, 'done', 1)",
        (prompt_id,),
    ).lastrowid
    item_id = conn.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'failed')",
        (job_id, e1),
    ).lastrowid  # request_format_version/request_fingerprint left NULL

    with pytest.raises(StaleOrSupersededRetryError):
        retry_failed_job_item(conn, item_id=item_id, owner_token="retry-owner")


def test_retry_failed_job_item_rejects_when_not_found_or_not_failed(conn):
    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    job_id = _enqueue_single_item_job(conn, prompt_id, e1)
    item_id = conn.execute("SELECT id FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchone()["id"]
    # Item is still 'pending', not 'failed'.
    with pytest.raises(StaleOrSupersededRetryError):
        retry_failed_job_item(conn, item_id=item_id, owner_token="retry-owner")
    with pytest.raises(StaleOrSupersededRetryError):
        retry_failed_job_item(conn, item_id=999999, owner_token="retry-owner")


def test_retry_failed_job_item_rejects_when_baseline_superseded(conn):
    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    job_id = _enqueue_single_item_job(conn, prompt_id, e1)
    item_id = conn.execute("SELECT id FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchone()["id"]
    release_lease(conn, get_lease_by_target(conn, entry_target_key(e1))["id"])
    fail_job_item(conn, item_id, "boom")
    conn.execute("UPDATE regen_job SET status = 'done' WHERE id = ?", (job_id,))
    # Newer work completed successfully for this same target AFTER this item was admitted --
    # its baseline (captured as NULL, no prior result) no longer matches the current result.
    conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'test-model', 'newer take', 'ok')",
        (e1, prompt_id),
    )

    with pytest.raises(StaleOrSupersededRetryError):
        retry_failed_job_item(conn, item_id=item_id, owner_token="retry-owner")


def test_retry_failed_job_item_rejects_when_newer_same_target_item_exists(conn):
    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    job_id = _enqueue_single_item_job(conn, prompt_id, e1)
    item_id = conn.execute("SELECT id FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchone()["id"]
    release_lease(conn, get_lease_by_target(conn, entry_target_key(e1))["id"])
    fail_job_item(conn, item_id, "boom")
    conn.execute("UPDATE regen_job SET status = 'done' WHERE id = ?", (job_id,))
    # A newer job item for the SAME target was created afterward (simulating a later apply-all
    # touching the same entry) -- direct row insert to avoid needing a second full job/lease
    # dance in this test.
    conn.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status, request_format_version, "
        "request_fingerprint) VALUES (?, 'entry_commentary', ?, 'pending', 1, 'fp-newer')",
        (job_id, e1),
    )

    with pytest.raises(StaleOrSupersededRetryError):
        retry_failed_job_item(conn, item_id=item_id, owner_token="retry-owner")


def test_retry_failed_job_item_rejects_when_target_busy(conn):
    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    job_id = _enqueue_single_item_job(conn, prompt_id, e1)
    item_id = conn.execute("SELECT id FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchone()["id"]
    # Do NOT release the admission-time lease -- simulate the target still busy somehow.
    fail_job_item(conn, item_id, "boom")
    conn.execute("UPDATE regen_job SET status = 'done' WHERE id = ?", (job_id,))

    with pytest.raises(TargetBusyError):
        retry_failed_job_item(conn, item_id=item_id, owner_token="retry-owner")


def test_retry_failed_job_item_rejects_when_another_job_is_running(conn):
    e1 = _seed_entry(conn, "e1")
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    job_id = _enqueue_single_item_job(conn, prompt_id, e1)
    item_id = conn.execute("SELECT id FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchone()["id"]
    release_lease(conn, get_lease_by_target(conn, entry_target_key(e1))["id"])
    fail_job_item(conn, item_id, "boom")
    conn.execute("UPDATE regen_job SET status = 'done' WHERE id = ?", (job_id,))

    # A different job is currently running (single-flight rule). Seeded AFTER job_id's own
    # preflight snapshot so it doesn't trip an unrelated ArchiveChangedError.
    e2 = _seed_entry(conn, "e2", "2026-02-01")
    other_job_id, _ = enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1, e2],
        targets=[PreparedRegenTarget("entry_commentary", e2, 1, "fp2")],
        owner_token="other",
    )
    assert other_job_id != job_id

    with pytest.raises(sqlite3.IntegrityError):
        retry_failed_job_item(conn, item_id=item_id, owner_token="retry-owner")


# ---------------------------------------------------------------------------
# Snapshot-aware startup recovery
# ---------------------------------------------------------------------------

def test_recover_or_cancel_running_jobs_resumes_snapshot_backed_job(conn):
    e1 = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    job_id = _enqueue_single_item_job(conn, prompt_id, e1)
    item_id = conn.execute("SELECT id FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchone()["id"]
    # Simulate a crash mid-item: item stuck 'running', its admission-time lease still present
    # (from a DEAD previous process).
    conn.execute("UPDATE regen_job_item SET status = 'running' WHERE id = ?", (item_id,))

    result = recover_or_cancel_running_jobs(conn, owner_token="new-process")

    assert isinstance(result, RecoveryResult)
    assert result.recovered_job_ids == [job_id]
    assert result.cancelled_job_ids == []
    item = conn.execute("SELECT status FROM regen_job_item WHERE id = ?", (item_id,)).fetchone()
    assert item["status"] == "pending"
    # The lease was re-acquired fresh under the NEW process's owner token.
    lease = get_lease_by_target(conn, entry_target_key(e1))
    assert lease is not None
    assert lease["owner_token"] == "new-process"


def test_recover_or_cancel_running_jobs_cancels_legacy_job_without_snapshot(conn):
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    e1 = _seed_entry(conn)
    job_id = conn.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'running')", (prompt_id,)
    ).lastrowid  # no snapshot_entry_count -> legacy, never resumable
    conn.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'running')",
        (job_id, e1),
    )
    conn.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'aggregate_report', NULL, 'pending')",
        (job_id,),
    )

    result = recover_or_cancel_running_jobs(conn, owner_token="new-process")

    assert result.recovered_job_ids == []
    assert result.cancelled_job_ids == [job_id]
    assert result.cancelled_item_count == 2
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"
    remaining_items = conn.execute(
        "SELECT COUNT(*) AS n FROM regen_job_item WHERE job_id = ?", (job_id,)
    ).fetchone()["n"]
    assert remaining_items == 0


def test_recover_or_cancel_running_jobs_clears_leases_from_previous_process(conn):
    # Stale leases from a dead process must never survive into the new process's lifetime.
    acquire_lease(conn, "entry:999", "direct", "dead-process")

    recover_or_cancel_running_jobs(conn, owner_token="new-process")

    assert get_lease_by_target(conn, "entry:999") is None


def test_recover_or_cancel_running_jobs_is_noop_when_nothing_was_running(conn):
    result = recover_or_cancel_running_jobs(conn, owner_token="new-process")
    assert result.recovered_job_ids == []
    assert result.cancelled_job_ids == []
    assert result.cancelled_item_count == 0


def test_recover_or_cancel_running_jobs_cancels_snapshot_backed_job_with_missing_item_identity(conn):
    # Even a job WITH a snapshot must be cancelled, not blindly resumed, if one of its unfinished
    # items is missing its request format version/fingerprint (e.g. corrupted state, or written by
    # code that forgot to populate them) -- recovery must never construct a placeholder
    # PreparedRegenTarget with a defaulted 0/"" identity to make it "fit".
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    e1 = _seed_entry(conn)
    job_id, _ = enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1],
        targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp")],
        owner_token="crashed",
    )
    # Corrupt the item's stored identity as if it were written by defective code.
    conn.execute(
        "UPDATE regen_job_item SET status = 'running', request_fingerprint = NULL WHERE job_id = ?",
        (job_id,),
    )

    result = recover_or_cancel_running_jobs(conn, owner_token="new-process")

    assert result.recovered_job_ids == []
    assert result.cancelled_job_ids == [job_id]
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"


def test_recover_or_cancel_running_jobs_cancels_job_with_snapshot_count_mismatch(conn):
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    e1 = _seed_entry(conn)
    job_id, _ = enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1],
        targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp")],
        owner_token="crashed",
    )
    conn.execute("UPDATE regen_job_item SET status = 'running' WHERE job_id = ?", (job_id,))
    # Corrupt the stored snapshot: delete its one row while snapshot_entry_count still says 1.
    conn.execute("DELETE FROM regen_job_entry_snapshot WHERE job_id = ?", (job_id,))

    result = recover_or_cancel_running_jobs(conn, owner_token="new-process")

    assert result.recovered_job_ids == []
    assert result.cancelled_job_ids == [job_id]
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM regen_job_item WHERE job_id = ?", (job_id,)
    ).fetchone()["n"] == 0


def test_recover_or_cancel_running_jobs_cancels_job_with_missing_snapshotted_entry(conn):
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    e1 = _seed_entry(conn)
    job_id, _ = enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1],
        targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp")],
        owner_token="crashed",
    )
    conn.execute("UPDATE regen_job_item SET status = 'running' WHERE job_id = ?", (job_id,))
    # Simulate corruption: the snapshotted entry no longer exists (this app never actually
    # deletes entries, but recovery must still defend against it).
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM diary_entry WHERE id = ?", (e1,))
    conn.execute("PRAGMA foreign_keys=ON")

    result = recover_or_cancel_running_jobs(conn, owner_token="new-process")

    assert result.recovered_job_ids == []
    assert result.cancelled_job_ids == [job_id]
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"


def test_recover_or_cancel_running_jobs_cancels_job_with_missing_prompt(conn):
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    e1 = _seed_entry(conn)
    job_id, _ = enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1],
        targets=[PreparedRegenTarget("entry_commentary", e1, 1, "fp")],
        owner_token="crashed",
    )
    conn.execute("UPDATE regen_job_item SET status = 'running' WHERE job_id = ?", (job_id,))
    # Simulate corruption: the job's own prompt version disappears.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM persona_prompt WHERE id = ?", (prompt_id,))
    conn.execute("PRAGMA foreign_keys=ON")

    result = recover_or_cancel_running_jobs(conn, owner_token="new-process")

    assert result.recovered_job_ids == []
    assert result.cancelled_job_ids == [job_id]
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"


def test_recover_or_cancel_running_jobs_cancels_job_with_invalid_aggregate_report_entry_id(conn):
    # An aggregate_report item must never carry an entry_id -- if one somehow does (corruption),
    # recovery must refuse to build a PreparedRegenTarget for it and cancel instead.
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    e1 = _seed_entry(conn)
    job_id, _ = enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=[e1],
        targets=[PreparedRegenTarget("aggregate_report", None, 1, "fp")],
        owner_token="crashed",
    )
    conn.execute(
        "UPDATE regen_job_item SET status = 'running', entry_id = ? WHERE job_id = ?",
        (e1, job_id),
    )

    result = recover_or_cancel_running_jobs(conn, owner_token="new-process")

    assert result.recovered_job_ids == []
    assert result.cancelled_job_ids == [job_id]
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"


def test_recover_or_cancel_running_jobs_cancels_job_whose_item_entry_is_valid_but_outside_snapshot(conn):
    # Regression test: a real, currently-existing diary_entry that is simply NOT part of this
    # job's own stored snapshot must be rejected exactly like a missing/corrupted entry_id --
    # the prior check only asked "is entry_id non-null", which a valid entry from OUTSIDE the
    # snapshot would satisfy, letting the job resume and eventually generate/fail against an
    # entry never actually admitted into this job's context.
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    e_in_snapshot = _seed_entry(conn, title="in snapshot", entry_date="2026-01-01")
    job_id, _ = enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=[e_in_snapshot],
        targets=[PreparedRegenTarget("entry_commentary", e_in_snapshot, 1, "fp")],
        owner_token="crashed",
    )
    # A second entry is written AFTER this job's snapshot was captured -- exactly the scenario
    # the plan requires to have zero effect on an already-enqueued job.
    e_outside_snapshot = _seed_entry(conn, title="outside snapshot", entry_date="2026-01-02")
    # Corrupt the item's entry_id to point at a real, existing entry that was never part of
    # this job's snapshot (bypass FK-adjacent app invariants directly at the DB layer, as the
    # normal app code never produces this state -- this is a defense-in-depth test).
    conn.execute(
        "UPDATE regen_job_item SET status = 'running', entry_id = ? WHERE job_id = ?",
        (e_outside_snapshot, job_id),
    )

    result = recover_or_cancel_running_jobs(conn, owner_token="new-process")

    assert result.recovered_job_ids == []
    assert result.cancelled_job_ids == [job_id]
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"
    # No lease was left behind for either entry.
    assert get_lease_by_target(conn, entry_target_key(e_in_snapshot)) is None
    assert get_lease_by_target(conn, entry_target_key(e_outside_snapshot)) is None
