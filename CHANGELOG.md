# Changelog

## 0.4.0-alpha.2 — Unreleased

This candidate has not been tagged, published, or deployed by this repository
state.

### Added

- Add CLI and MCP reference guides, plus a generic Linux deployment template.
- Add local CORTEX operation inspection and owner-authorized retry for idempotent
  uncertain notes.
- Add bounded audit review checkpoints and verification of new audit hashes,
  including event IDs and server timestamps.
- Add Linux CI on Python 3.13, deployment syntax checks, isolated test homes,
  resource-warning failures, and regression tests for recovery and client boundaries.

### Changed

- Require SQLite 3.42 or newer for secure FTS deletion behavior. Schema v1 stores
  require an offline migration; existing schema v2 stores retain their version.
- Make backup creation standalone and atomic, and preserve a safe recovery state
  when restore cutover validation fails.
- Bound semantic indexing to 128 chunks per memory, exclude low-similarity
  results, and retain full-text excerpts in hybrid search.
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
- Preserve typed MCP errors, strict input types, and confirmed mutation results
  when the later audit receipt cannot be saved.
- Keep capture moving past terminally rejected records and detect file rotation
  without replaying ordinary appends.
- Preserve CORTEX operation ownership and treat post-send failures as uncertain.
- Close backup connections, securely remove forgotten full-text terms, and avoid
  adopting incomplete databases.
- Coordinate managed and supervised writers, preserve recovery markers during
  uninstall, and prevent an older client from replacing a newer daemon.

### Breaking

- Existing stores backed by unsupported SQLite versions cannot be safely upgraded
  or rolled back through this candidate.
- CLI and MCP callers should use structured error codes and the updated result
  contracts. A boolean or numeric string is no longer accepted as a revision.
- OAuth configure/disable no longer starts or restarts a daemon implicitly.

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

## Known limits

- Public ingress, provider account setup, continuous conversation capture, native
  client UI activation, off-host recovery, and reboot acceptance are not verified.
