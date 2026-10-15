# Changelog

## 0.4.2

- Replace obsolete verification history with reproducible source, installed-wheel,
  Linux, retrieval, and deployment checks.
- Correct release workflow and bootstrap resume guidance; consolidate historical
  deployment pointers into current operating documentation.
- Align package descriptions, contributor commands, and troubleshooting with the
  implemented behavior.
- Report history import, continuous and provider-wide capture as disabled in memory status.
  Storage, retrieval, schema, and dependencies are unchanged.

## 0.4.1

- Preserve a concurrently replaced credential or export file when creation fails.
- Report incomplete output cleanup while retaining the original error and keeping
  private diagnostic details out of CLI output.
- Keep semantic chunks in deterministic memory order so candidate pruning retains
  the best body passage for a title match.
- Correct setup guidance: capture and the CORTEX connector remain disabled and
  excluded from the core package. Schema v2 and dependencies are unchanged.

## 0.4.0

- Retain schema v2 and the explicit released v1 migration. Add read-only version
  checks, FTS secure deletion, snapshot/recovery validation and writer/HTTP leases.
- Preserve OAuth client/grant provenance after invalidation; bind public policy
  to its listener and make bulk access changes explicit offline maintenance.
- Reject known secrets, warn on uncertain content, retain successful writes when
  later auditing fails, and report unknown remote outcomes without automatic retry.
- Return the source body/revision with title matches; make indexing counts and
  FTS fallback explicit and isolate bridge sessions for concurrent calls.
- Publish managed state after readiness; bind process control to its data path.
  Add read-only connect with failure rollback and explicit supervisor endpoints,
  independent doctor checks, timestamps and one startup log rotation.
- Add ownership-aware disconnect and uninstall that preserve unrelated client
  settings, changed credentials and retained memory data.
- Add schema-v2 audit review with checkpoint continuity and operator diagnostics.
- Exclude disabled CORTEX and capture modules and their tests from the core
  distribution while preserving their source-only characterizations.
- Add installed-wheel verification using a released synthetic v1 fixture and
  real stdio MCP.

This version uses selected changes from the later experimental branch; its
v3/v4 schemas and additional command surfaces are not part of this v2 result.

## 0.4.0-alpha.1 — 2026-10-09

The existing tag is retained; it is not the batch-1 candidate.

### Added

- Add project-scoped identities, authenticated revision authorship, passage
  embeddings, a sanitized action audit, and an experimental JSONL snapshot importer.
- Add optional CORTEX context and publication of a selected memory revision.
- Add validated backups, staged restores, deletion reconciliation, and the
  operator audit review helper.

### Breaking

- Schema v2 requires an explicit offline migration from v1.
- `memory_forget` requires the caller's observed `expected_revision`.

## 0.3.0-alpha.2 — 2026-10-04

- Tighten OAuth issuer validation and supersede the alpha.1 release candidate.

## 0.3.0-alpha.1 — 2026-10-04

- Add optional OAuth discovery, dynamic registration, PKCE, scoped owner approval,
  refresh, and revocation using the MCP SDK.

## 0.2.0-alpha.2 — 2026-10-02

- Add previewable uninstall and disconnect while retaining memories and unrelated
  client settings. Require explicit resume after uninstall.

## 0.2.0-alpha.1 — 2026-10-02

- Add managed Linux startup, local client adapters, bridge verification, and
  bounded storage, HTTP, and semantic stress scripts.

## 0.1.0-alpha.2 — 2026-10-02

- Bound and retry first-run WAL lock contention without losing existing data.
- Let both Python CI jobs finish when one matrix job fails.

## 0.1.0-alpha.1 — 2026-10-01

- Introduce SQLite source records, revisions, project-filtered full-text search,
  exports and deletion suppression.
- Add six MCP tools, stdio and authenticated loopback HTTP, local credentials,
  a bridge and machine-readable setup commands.
- Add pinned multilingual CPU embeddings, background indexing and hybrid search.
- Add the bootstrap and onboarding skill, release validation and Python 3.11/3.12 CI.

## Known limits

- Public ingress, provider account setup, continuous conversation capture, native
  client UI activation, off-host recovery, and reboot acceptance are not verified.
