# Repository guidance

- Run the full test suite with `.venv/bin/pytest -q`. Use the narrowest relevant tests while
  developing.
- Keep the nine translation catalogs in exact key parity. Import-time validation rejects a missing
  or extra key.
- Keep public copy English-only. Do not use emoji, em or en dashes, or inaccurate licensing claims.
  Describe the project as source-available for noncommercial use.
- Use only fictional journal content in public fixtures, screenshots, tests, documentation, and
  issue examples. Never copy private entries, tokens, hostnames, databases, or personal identifiers
  into the repository.
- Rebuild and verify `site/assets/images/provenance.json` whenever a public image changes.
- Follow [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, pull requests, and the noncommercial
  contribution license.
