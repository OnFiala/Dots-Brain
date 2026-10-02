# Changelog

## Unreleased

- Add a manually triggered GitHub release workflow that verifies an existing
  annotated tag, builds on both supported Python versions, and uploads checked
  packages to a draft without overwriting published assets.
- Document the repeatable release procedure.

## 0.1.0-alpha.2 — 2026-10-02

- Fix intermittent first-run failures when concurrent installers enable SQLite WAL.
  Journal-mode lock contention now retries within a bounded deadline, while other
  errors still fail immediately. The existing schema transaction remains atomic.
- Test temporary lock recovery and timeout recovery without losing existing data.
- Let both supported Python versions finish CI even when one matrix job fails.

## 0.1.0-alpha.1 — 2026-10-01

- Establish the product contract, English documentation, and MIT license.
- Add transactional SQLite memory, provenance, explicit revisions, project-filtered
  full-text search, export, and deletion with reimport suppression.
- Verify concurrent retry handling, restart persistence, and project isolation.
- Add six MCP tools, stdio and authenticated loopback HTTP, scoped client
  credentials, revocation, a stdio-to-HTTP bridge, and machine-readable CLI setup.
- Add pinned multilingual CPU embeddings with artifact verification, automatic
  background indexing, and hybrid retrieval; no paid inference API is used.
- Add a deterministic bootstrap, an onboarding skill package, English operating
  documentation, release validation, and GitHub CI for Python 3.11 and 3.12.
- Verify a live HTTP process through the stdio bridge and fresh source installation.

### Known limits

- The HTTP service binds loopback and does not implement web OAuth or a tunnel.
- Provider capture, automatic remote configuration, native ChatGPT UI, and MCP
  Events are not implemented. Dot VM persistence, costs, and strong credential
  isolation are not verified.
- The embedding smoke test is not a Cortex comparison or a production benchmark.
