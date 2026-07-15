"""Command-line entry points for import, release maintenance, and the synthetic deploy probe."""
import argparse
import asyncio
import json
import sqlite3
import sys

from unflincher.db import (
    ACTIVE_PROMPT_MANIFEST_FIELDS,
    active_prompt_manifest,
    enable_wal_mode,
    get_bootstrap_state,
    get_connection,
    get_existing_connection,
    get_generation_activity,
    get_maintenance_locked,
    initialize_database,
    initialize_upgrade_database,
    install_prompt_identity_guard,
    lock_maintenance_for_bootstrap,
    prompt_identity_manifest,
    remove_prompt_identity_guard,
    require_generation_idle,
    require_v02_operational_schema,
    set_maintenance_locked,
    unlock_maintenance_if_idle,
    verify_current_result_selection_compatibility,
    verify_v01_upgrade_schema,
)
from unflincher.importer import MissingColumnsError, import_excel

def _database_status(conn: sqlite3.Connection) -> dict[str, object]:
    bootstrap_state = get_bootstrap_state(conn)
    if bootstrap_state is None:
        raise RuntimeError("offline bootstrap has not completed")
    return {
        "active_prompt": active_prompt_manifest(conn),
        "bootstrap_state": bootstrap_state,
        "maintenance": {
            "locked": get_maintenance_locked(conn),
            **get_generation_activity(conn),
        },
    }


def _print_database_status(label: str, payload: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return

    maintenance = payload["maintenance"]
    bootstrap = payload["bootstrap_state"]
    print(
        f"{label}: locked={str(maintenance['locked']).lower()} "
        f"active_leases={maintenance['active_lease_count']} "
        f"running_jobs={maintenance['running_regen_job_ids']} "
        f"fresh_install={str(bootstrap['is_fresh_install']).lower()} "
        f"analyst_seeded={str(bootstrap['analyst_seeded']).lower()} "
        "current_result_selection_verified="
        f"{str(bootstrap['current_result_selection_verified']).lower()}"
    )
    manifest = payload["active_prompt"]
    if manifest is None:
        print("active_prompt=none")
        return
    for field in ACTIVE_PROMPT_MANIFEST_FIELDS:
        value = manifest[field]
        print(f"active_prompt.{field}={'NULL' if value is None else value}")


def _run_bootstrap(db_path: str) -> dict[str, object]:
    conn = get_existing_connection(db_path)
    try:
        verify_v01_upgrade_schema(conn)
        require_generation_idle(conn)
        prompts_before = prompt_identity_manifest(conn)
        bootstrap_state = get_bootstrap_state(conn)
        if bootstrap_state is not None:
            if bootstrap_state["is_fresh_install"]:
                raise RuntimeError("bootstrap is only valid for an upgraded v0.1 database")
            if not bootstrap_state["analyst_seeded"]:
                raise RuntimeError("db_bootstrap_state.analyst_seeded is not upgrade-safe")
        if (
            bootstrap_state is None
            or not bootstrap_state["current_result_selection_verified"]
        ):
            verify_current_result_selection_compatibility(conn)

        lock_maintenance_for_bootstrap(conn)
        enable_wal_mode(conn)
        install_prompt_identity_guard(conn)
        try:
            initialize_upgrade_database(conn)
        finally:
            remove_prompt_identity_guard(conn)

        prompts_after = prompt_identity_manifest(conn)
        if prompts_after != prompts_before:
            raise RuntimeError("persona_prompt identities changed during bootstrap")
        require_v02_operational_schema(conn)
        return _database_status(conn)
    finally:
        conn.close()


def _run_maintenance(
    action: str,
    db_path: str,
) -> dict[str, object]:
    conn = get_existing_connection(db_path)
    try:
        require_v02_operational_schema(conn)
        if action == "lock":
            set_maintenance_locked(conn, True)
        elif action == "unlock":
            unlock_maintenance_if_idle(conn)
        return _database_status(conn)
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m unflincher.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="import a 豆伴 Excel export")
    import_parser.add_argument("--excel", required=True)
    import_parser.add_argument("--db", required=True)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="offline, locked v0.1-to-v0.2 database bootstrap",
    )
    bootstrap_parser.add_argument("--db", required=True)
    bootstrap_parser.add_argument("--json", action="store_true")

    maintenance_parser = subparsers.add_parser(
        "maintenance",
        help="inspect or change the v0.2 generation maintenance gate",
    )
    maintenance_subparsers = maintenance_parser.add_subparsers(
        dest="maintenance_action",
        required=True,
    )
    for action in ("status", "lock", "unlock"):
        action_parser = maintenance_subparsers.add_parser(action)
        action_parser.add_argument("--db", required=True)
        action_parser.add_argument("--json", action="store_true")
        if action == "unlock":
            action_parser.add_argument(
                "--confirm-service-healthy",
                action="store_true",
                required=True,
                help="confirm health, revision, migration, and probe checks already passed",
            )

    probe_parser = subparsers.add_parser(
        "probe",
        help="local-only synthetic deployment health probe (no database access, no HTTP route)",
    )
    probe_parser.add_argument(
        "--model", default=None,
        help="model ID to probe (defaults to UNFLINCHER_LLM_MODEL / config default)",
    )

    args = parser.parse_args(argv)

    if args.command == "import":
        conn = get_connection(args.db)
        try:
            # Same one deep interface app.py's lifespan uses (see db.initialize_database) -- this
            # may be the very first process to touch the database file.
            initialize_database(conn)
            count = import_excel(args.excel, conn)
        except (MissingColumnsError, OSError, sqlite3.Error, RuntimeError) as exc:
            print(f"Import failed: {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        print(f"Imported {count} diary entries from {args.excel} into {args.db}")
        return 0

    if args.command == "bootstrap":
        try:
            payload = _run_bootstrap(args.db)
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            print(f"Bootstrap failed: {exc}", file=sys.stderr)
            return 1
        _print_database_status("Bootstrap ok", payload, args.json)
        return 0

    if args.command == "maintenance":
        try:
            payload = _run_maintenance(args.maintenance_action, args.db)
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            print(f"Maintenance {args.maintenance_action} failed: {exc}", file=sys.stderr)
            return 1
        _print_database_status(
            f"Maintenance {args.maintenance_action} ok",
            payload,
            args.json,
        )
        return 0

    if args.command == "probe":
        from unflincher import llm as llm_module
        from unflincher.config import load_settings
        from unflincher.probe import run_probe

        model = args.model or load_settings().llm_model

        async def _run() -> str:
            try:
                return await run_probe(model)
            finally:
                # One-shot CLI invocation: always tear the client down before exiting, whether
                # the probe succeeded or failed.
                await llm_module.shutdown_client()

        try:
            reply = asyncio.run(_run())
        except Exception as exc:
            print(f"Deployment probe failed (model={model}): {exc}", file=sys.stderr)
            return 1
        print(f"Deployment probe ok (model={model}): {reply}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
