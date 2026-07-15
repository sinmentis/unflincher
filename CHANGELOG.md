# Changelog

All notable changes to this project are documented here. This project follows semantic versioning.

## Unreleased (target 0.2.0)

- Repositioned Unflincher as an evidence-grounded AI reflection partner for people with years of
  journal entries.
- Added Companion, Coach, Challenger, and Analyst Perspective presets, plus preserved Custom
  instructions. Analyst is the default only for a new database.
- Renamed user-facing commentary and chat surfaces to Entry Reflection and Conversations while
  preserving stable routes and existing generated content.
- Rebuilt Prompt Workshop around Perspective choice, instruction tuning, one-entry preview, and
  explicit apply behavior.
- Added data-derived onboarding for empty and imported Journal Archives.
- Added exact request preflight against published model limits. Oversized requests fail clearly and
  never silently drop older archive entries or Conversation history.
- Added ordered archive snapshots, request fingerprints, exclusive generation leases, safer retry
  and recovery behavior, and a maintenance gate for controlled deployment.
- Added fail-locked v0.1 bootstrap, immutable image identity, exact-image deployment, verified
  rollback tags, offline backup drills, and revision-aware health checks.
- Rebuilt the fictional public demo and landing page around dated Entry References, Perspectives,
  Conversation, accurate data-flow disclosures, and the non-therapy boundary.
- Added visible FAQ content with matching structured data, factual application features,
  `llms.txt`, sitemap dates, and expanded public-copy auditing.

## 0.1.0 (2026-07-15)

- First source-available release.
- Private application crawler defenses: a noindex meta tag, an X-Robots-Tag response header, and a
  disallow-all robots.txt behind Cloudflare Access.
- Public GitHub Pages site with a five-view static demo on fictional data.
- Community documentation: security policy, contributing guide, code of conduct, and templates.
