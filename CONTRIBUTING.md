# Contributing

Thanks for your interest in Unflincher.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

## Tests

Run the full suite before opening a pull request:

```bash
.venv/bin/pytest -q
```

The only accepted baseline warning is the Starlette httpx TestClient deprecation. Treat any other
warning as new.

## Issue scope

Unflincher is a single-user, self-hosted reflection tool. Requests that turn it into a hosted
multi-user service, add accounts, or add a public writable demo are out of scope. Bug reports,
documentation fixes, accessibility improvements, and self-hosting quality-of-life changes are
welcome.

## License

By contributing you agree that your contributions are licensed under the PolyForm Noncommercial
License 1.0.0. Unflincher is source available for noncommercial use and is not licensed for
commercial use.
