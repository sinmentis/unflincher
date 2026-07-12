"""Command-line entry points. Currently just the one-off historical import (run once,
before the web service is first started, per technical design §7.4)."""
import argparse
import sys

from unflincher.db import get_connection, init_schema
from unflincher.importer import MissingColumnsError, import_excel


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m unflincher.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="import a 豆伴 Excel export")
    import_parser.add_argument("--excel", required=True)
    import_parser.add_argument("--db", required=True)

    args = parser.parse_args(argv)

    if args.command == "import":
        conn = get_connection(args.db)
        init_schema(conn)
        try:
            count = import_excel(args.excel, conn)
        except MissingColumnsError as exc:
            print(f"Import failed: {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        print(f"Imported {count} diary entries from {args.excel} into {args.db}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
