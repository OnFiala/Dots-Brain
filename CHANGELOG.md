# Changelog

## 0.4.0-alpha.2 — Unreleased

This candidate has not been tagged, published, or deployed by this repository
state.

### Added

- Add CLI and MCP reference guides, plus a generic Linux deployment template.
- Add local CORTEX operation inspection, explicit uncertain-delivery resolution,
  and guarded recovery of an interrupted `sending` receipt.
- Add bounded audit review checkpoints, adaptive page reduction, and verification
  of new audit hashes including event IDs and server timestamps.
- Add Linux CI on Python 3.13, deployment syntax checks, isolated test homes,
  resource-warning failures, and regression tests for recovery and client boundaries.
- Exercise the installed wheel outside the checkout, Chromium OAuth callbacks and
  the pinned embedding model in separate CI jobs.

### Changed

- Require SQLite 3.42 or newer for secure FTS deletion behavior.
- Use one versioned schema definition for setup and offline migration. Both v1
  and v2 stores require migration to v3, including OAuth and semantic index state.
- Make backup creation standalone and atomic, and preserve a safe recovery state
  when restore cutover validation fails.
- Bound semantic indexing to 128 chunks per memory, exclude low-similarity
  results, and retain the best semantic body passage in hybrid search.
- Reuse the local bridge connection and report overload without replaying writes.
- Close OAuth onboarding by default. Require the redirect host at approval and
  explicit confirmation before replacing the issuer.

### Fixed

- Add a partial abort-recovery command for a failed cutover. It requires the
  documented offline operator checks and cancels only a pending cutover.
- Replace owner-specific deployment records with public Linux installation and
  ingress guidance.
- Bind OAuth pairing to its initiating browser; consume approval only on POST.
  Preserve refresh scope and recover interrupted configuration changes.
- Omit client names from default OAuth request inspection. Show them only with
  `--verbose` as untrusted client text.
- Bound text and JSON keys before credential matching. Minimize CORTEX context
  outside the request thread.
- Preserve typed MCP errors, strict input types, and confirmed mutation results
  when the later audit receipt cannot be saved.
- Keep capture moving past terminally rejected records and detect replacement or
  rewrite without automatically replaying acknowledged rows.
- Preserve CORTEX operation ownership, retain superseded pre-send outcomes as
  terminal history, and treat post-send failures as uncertain.
- Close backup connections, securely remove forgotten full-text terms, and avoid
  adopting incomplete databases.
- Coordinate managed and supervised writers, preserve recovery markers during
  uninstall, and prevent an older client from replacing a newer daemon.
- Replace incompatible historical installer probes and revoke their old credentials.
- Bound bridge concurrency and request deadlines; require full probe provenance
  before cleanup and preserve unresolved recovery receipts.
- Keep normal long queries usable, report context clipping, and share the model's
  tokenizer to avoid a second large native allocation.
- Remove unlinked host-specific benchmark snapshots and obsolete internal paths.

### Breaking

- Old v1/v2 stores report `migration_required` until an explicit offline upgrade.
  Older binaries reject schema v3. See [upgrade and rollback](docs/upgrading.md).
- Existing stores backed by unsupported SQLite versions cannot be safely upgraded
  or rolled back through this candidate.
- CLI and MCP callers should use structured error codes and the updated result
  contracts. A boolean or numeric string is no longer accepted as a revision.
- OAuth configure/disable no longer starts or restarts a daemon implicitly.
- A listener started with `--public-gateway` accepts OAuth grants only. Static
  credentials require a private listener; request headers cannot change the policy.
- Setup on a disabled instance fails. Only the explicit recovery or reinstall
  command can clear its marker. `doctor` returns separate database, OAuth and
  managed-service checks with a top-level `healthy` result.
- `client create` needs `--url` unless a managed endpoint is recorded. New client
  credentials default to 30 days; existing credentials retain their expiry.
- `audit_review.py` requires a private `target.json`. Capture stops on an
  acknowledged-prefix rewrite instead of replaying potentially forgotten records.
- MCP is pinned below 1.31 because the typed dispatch boundary depends on the
  tested SDK handler contract. Unsupported hard-link filesystems are rejected.

## 0.4.0-alpha.1 — 2026-10-09

The existing tag is retained. It predates the audit fixes above.

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
