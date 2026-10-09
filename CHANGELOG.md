# Changelog

## 0.4.0-alpha.1 — 2026-10-09

- Add schema v2 with project-scoped source identities, server-authenticated revision
  authorship and deletion barriers. Upgrade explicitly with stopped writers and a
  verified backup; preserve unknown legacy authorship.
- Index title and content passages separately. Return useful body context when a
  semantic match lands on a short title, while preserving the ranked revision and
  caller's character budget.
- Reject known credential patterns from memory input. Add sanitized append-only
  action audit, mutation intents and receipts, exact deletion targets, and bounded
  resumable JSONL snapshot collection with coverage gaps.
- Add one-to-one CORTEX project mapping, bounded context and selected publication
  of an exact source revision. Preserve acknowledgement IDs during reconciliation;
  report failed receipt persistence as uncertain and prevent blind replay.
- Add validated SQLite backups, disabled staged restores, credential revocation,
  deletion reconciliation and cutover guards against losing newer canonical state.
- Preserve OAuth pairing provenance through code exchange without changing legacy
  table layouts or widening existing grants. Support clients that omit registration
  scopes and subsequently request read/write; exact owner approval is still required.
- Deploy an isolated Linux systemd service, offline CPU model and constrained HTTPS
  ingress. Verify actual Botter/Grok OAuth read/write, cross-bot reads, initial
  contributions and retained access after an upgrade.
- Require the reviewed revision when deleting. Keep deletion retries idempotent and
  reject stale deletes atomically. Custom clients must refresh tool discovery.
- Validate the existing store, credential and saved loopback endpoint before local
  resume. Add a schema-discovering MCP shell client without creating another database.
- Add the twice-daily operator review helper with complete-range analysis and durable
  checkpoints. Manual review is verified; unattended network access remains blocked
  in the observed restricted execution context.

- Pin CI/release Actions to full commits and validate those pins before release.
  Record bounded appliance storage, HTTP and real-model stress measurements.

Live CORTEX upstream access, full provider capture, off-host recovery and real
appliance reboot acceptance remain open. See [capabilities](docs/capabilities.md)
for implementation and runtime evidence separately.

## 0.3.0-alpha.2 — 2026-10-04

- Reject IPv6 HTTP issuer origins before changing configuration: SDK 1.30 does
  not support them. HTTPS issuers and exact localhost/127.0.0.1 development origins remain supported.
- Reject issuer URLs with an empty user-information delimiter as well as real credentials.
- Add regression cases and clarify the supported development origins.
- Supersede the alpha.1 release candidate before publication; its Git tag is preserved.

## 0.3.0-alpha.1 — 2026-10-04

- Add optional VM-local OAuth to the existing HTTP process and SQLite database,
  using official MCP SDK handlers with no new runtime dependency.
- Add discovery, dynamic registration, S256 PKCE, resource-bound code exchange,
  scoped project grants, rotating refresh tokens, and client/owner revocation.
- Add deterministic owner commands to inspect, approve, deny, and revoke a specific
  connection without printing its tokens. Default approval excludes deletion and
  can narrow the client's requested scopes; all-project access remains explicit.
- Persist grants across restart and invalidate pending flows and refresh tokens
  on OAuth disable, issuer changes, and whole-instance uninstall.
- Bound registration metadata, request bodies, client count, and pending flows;
  reject invalid callback schemes and malformed registration JSON.
- Enforce token-request resource binding and normalize the optional public-client
  revocation field around the SDK's current handlers. Accept case-insensitive
  HTTP Bearer scheme names.
- Require the tested MCP SDK 1.30 or newer within the existing major version.
- Verify 17 OAuth tests, including the official client's full flow and automatic
  refresh over live local HTTP, concurrent code reuse, scope/project boundaries,
  expiry, revocation, capacity recovery, and retained-data removal.
- Document the agent workflow, storage boundaries, and remaining ingress/provider
  requirements. Public deployment and actual web-provider setup remain unverified.

## 0.2.0-alpha.2 — 2026-10-02

- Add repeatable `uninstall` with a read-only preview, host credential revocation,
  managed-process stopping, and removal of unchanged registered client entries.
- Preserve memories, revisions, deletion suppression, models, unrelated client
  settings, and modified or unreadable configurations; report incomplete cleanup.
- Block automatic restart after removal until explicit `up --resume` or reinstall.
- Add `disconnect`, an integration inventory, and separate credentials for newly
  configured profiles. Recognize unchanged legacy entries and explicit custom paths.
- Preserve user-supplied remote credentials and report required issuer revocation.
- Document software removal as a separate step; add uninstall, upgrade, everyday
  usage, troubleshooting, and documentation-index guides in English.
- Extend the setup skill to cover removal and upgrades, and validate links across
  root documentation, nested guides, and skills.
- Verify 47 tests on Python 3.11 and 3.12 with one unchanged opt-in model test
  skipped; verify clean bootstrap, real Claude Code health, removal, reinstall,
  data retention, and removal of a disposable test checkout and package.

## 0.2.0-alpha.1 — 2026-10-02

- Add repeatable Linux background startup, crash recovery on local client reconnect,
  generated-bridge verification, and private credential reuse.
- Add Claude Code, Cursor, Codex, and generic MCP configuration adapters with
  preserved settings and explicit application-activation status.
- Extend the bootstrap to start the service and optionally connect a client.
- Keep MCP reads responsive while SQLite writers wait, and bound read/write workers separately.
- Avoid scanning the full-text index when inserting a new source record.
- Drain embedding backlogs in short bounded batches instead of sleeping between every batch.
- Recover the canonical port after a killed process finishes releasing its sockets.
- Add bounded synthetic storage, HTTP, and multilingual embedding stress harnesses.
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
