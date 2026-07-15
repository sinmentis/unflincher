"""Command-line entry points. Currently the one-off historical import (run once, before the web
service is first started, per technical design §7.4) and the local-only synthetic deployment
probe used by the deploy procedure (see probe.py's module docstring for the maintenance-bypass
rationale — it never touches the database)."""
import argparse
import asyncio
import sys

from unflincher.db import get_connection, initialize_database
from unflincher.importer import MissingColumnsError, import_excel


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m unflincher.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="import a 豆伴 Excel export")
    import_parser.add_argument("--excel", required=True)
    import_parser.add_argument("--db", required=True)

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
        except MissingColumnsError as exc:
            print(f"Import failed: {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        print(f"Imported {count} diary entries from {args.excel} into {args.db}")
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
