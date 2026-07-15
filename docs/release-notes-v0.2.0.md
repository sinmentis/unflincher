# Release notes: v0.2.0

Prepared: 2026-07-16. Not published.

This v0.2.0 release reframes Unflincher as an evidence-grounded AI reflection partner for
people with years of journal entries. It reads across a Journal Archive, names dated Entry References
behind an interpretation, and keeps that interpretation open to Conversation.

## Reflection Perspectives

Prompt Workshop now includes five Perspective choices:

- **Companion** reflects emotional reality first, then widens the reading without hiding a clear
  pattern.
- **Coach** connects supported patterns to decisions, goals, and small next steps.
- **Challenger** names contradiction, avoidance, and moving excuses without attacking identity or
  worth.
- **Analyst** separates observation from interpretation with minimal editorializing.
- **Custom** preserves instructions that do not exactly match a shipped preset.

Analyst is the default only for a new database. An existing active prompt remains byte-for-byte
unchanged, remains active, and appears as Custom after migration. Changing Perspective affects
future Entry Reflections, Life Reports, and Conversations only. Existing generated content and
Conversation messages are never rewritten automatically.

## Product experience

- Entry Reflection replaces the former commentary language.
- Conversations replaces the former reflective chat language.
- Prompt Workshop now guides Perspective choice, instruction tuning, a one-entry preview, and
  explicit apply or apply-and-regenerate behavior.
- Empty and imported Journal Archives receive data-derived onboarding without a wizard or tutorial
  state.
- The supported archive import remains an untouched Douban diary Excel export from the Tofu Chrome
  extension through the CLI importer. There is no browser upload or generic spreadsheet importer.

## Generation safety and reliability

- Every generation path prepares the exact request before streaming or durable domain writes.
- The request is checked against the selected model's context window. Unflincher fails clearly and
  never silently drops older entries or Conversation history.
- Background regeneration stores an ordered Journal Archive snapshot, request format version, and
  request fingerprint before work begins.
- Retry and crash recovery reject stale, superseded, snapshot-less, or format-changed work rather
  than generating from unverified context.
- Exclusive target and Conversation leases prevent overlapping writes and out-of-order turns.
- The maintenance gate blocks new generation while admitted work drains for deployment. A separate
  local synthetic probe can verify the model path without reading the Journal Archive or writing to
  the database.

## Public story and discoverability

- The fictional five-view demo now shows Entry Reflection, Life Report, Conversation, Prompt
  Workshop, all four shipped presets, and a Custom example.
- The GitHub Pages landing page now leads with the reflection-partner value, dated evidence,
  Perspective choice, Conversation, supported import, exact generation payloads, and context limits.
- Visible FAQ answers match the `FAQPage` structured data exactly.
- `SoftwareApplication.featureList`, `site/llms.txt`, sitemap dates, and broader public-copy audits
  improve machine-readable discovery without adding tracking or new indexed pages.
- Every public journal example and screenshot remains fictional and provenance-pinned.

## Privacy and product boundary

Entries, prompts, generated output, reports, and Conversation history stay in local SQLite. GitHub
Copilot remains the only model integration and receives the feature-specific context documented in
the README and public privacy matrix.

The public demo contains only fictional data and performs no model calls, tracking, cookies,
storage, or writable operations. The public site is hosted on GitHub Pages and is subject to
GitHub's platform logging and privacy practices.

Unflincher is not therapy, does not diagnose or treat, and does not impersonate a licensed
professional. It does not replace professional care or relationships with other people.

## Upgrade status

The schema migration is additive. Existing prompt versions, Entry Reflections, Life Reports, journal
entries, and Conversation messages remain in place.

An existing v0.1 database must use the fail-locked procedure in
[upgrade-v0.2.md](upgrade-v0.2.md). Ordinary v0.2 startup refuses to migrate it. The release tooling
builds an exact SHA-tagged image, verifies a pristine backup and disposable restore, performs the
offline bootstrap with maintenance locked, checks prompt and entry preservation, verifies the
running revision and local synthetic model probe, and unlocks generation only after every check
passes.

## License

Source available for noncommercial use under PolyForm Noncommercial 1.0.0. Commercial use requires a
separate license.
