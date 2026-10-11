# Changelog

## 0.4.0-alpha.4 — Unreleased

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
- Use one versioned schema definition for setup and offline migration. V1/v2/v3
  stores require migration to v4, including indexed IDs in the derived FTS table.
- Make backup creation standalone and atomic, and preserve a safe recovery state
  when restore cutover validation fails.
- Bound semantic indexing to 128 chunks per memory, exclude low-similarity
  results, and retain the best semantic body passage in hybrid search.
- Reuse the local bridge connection and report overload without replaying writes.
- Close OAuth onboarding by default. Require the redirect host at approval and
  explicit confirmation before replacing the issuer.

### Fixed

- Avoid creating disabled state when uninstalling a missing or empty installation;
  keep process-stop evidence unknown when it cannot be established. Scope
  disconnect to its requested client and report unrelated discovery as notes.
- Inspect Linux leases without acquiring them, report matching untracked servers,
  and refuse migration/startup while a detected old server remains. Preserve
  process ownership checks and include lease/PID evidence in readiness failures.
- Name actionable recovery commands for disabled stores, old backup schemas and
  client conflicts; identify parser-owned option names without echoing values.
- Progress past large accepted audit events through an explicit host-verified
  review projection, retain original hashes and record omitted content as a finding.
  Verify the same projection when it becomes the next scan's anchor.
- Version capture cursors and review state, validate legacy upgrades before writes,
  and preserve unsupported state with concrete diagnostic/recovery commands.
  Document capture's path/account/project suppression boundary.
- Aggregate semantic status without loading vectors, drain late vector repairs
  through CLI, and back off failed embeddings with a global retry rate limit.
- Preserve Unicode word boundaries and short boundary terms in long queries;
  report omitted terms and check the actual body before including title hits.
- Remove all duplicate FTS copies by indexed UUID plus exact identity, retaining
  a safe equality fallback for unusual historical IDs and damaged rowid maps.
- Isolate oversized bridge requests, enforce the deadline immediately before
  sending, and classify shared-session loss per call without replaying writes.
  Check current caller policy independently of the SDK discovery cache.
- Recover existing v1 probe receipts without replay; provide an explicit private
  archival path when a remote outcome cannot be resolved.
- Bind OAuth-only access to a second listener in the same supervised service,
  preserve concurrent static access on its private port, and ignore transport
  proxy headers. Report invalid OAuth state and recover readable configuration
  journals without revoking pre-commit grants. Bound ungranted registrations,
  preserve valid pending clients on purge, and validate callback origins before
  SDK normalization. Prevent pairing double-submit and support IPv6 callbacks.
- Replace the semantic calibration fixture with independently authored neutral
  synthetic objects and record its provenance. Re-measure both the baseline and
  candidate, including the current inability to reject its unrelated questions.
  Published branch history is retained.
- Accept historical v1/v2 stores touched by `7085841`, comparing SQL tokens
  against frozen historical DDL while preserving constraints and literals.
- Validate canonical data during migration planning, reject unsupported stores
  before a writable connection, and remove only owned automatic backups after a
  proved rollback. Document retained explicit snapshots and interrupted attempts.
- Replace backtracking credential rules with bounded category scans. Preserve
  quoted literal boundaries during capture redaction and return safe warnings
  for uncertain memory/CORTEX inputs without changing accepted content.
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

- End automatic support for unregistered 0.2.0-alpha.1 client entries. Report the
  exact file and manual cleanup steps; retain registered 0.3+ clients and schema
  v1 onward migration support. A non-Linux existing lease without kernel evidence
  now reports unknown; `down` returns exit 1 instead of claiming it stopped.
- Capture cursors and private review state now use explicit format version 1.
  The review helper requires `audit events --review-projection` on the memory host;
  it never silently falls back to an incompatible helper. Preserve older state
  copies and follow the documented upgrade/rollback path.
- Advance the package to `0.4.0a4` for schema v4; `0.4.0a3` used v3 and earlier
  `0.4.0a2` builds reused a version across formats. All v1/v2/v3 stores require
  an explicit offline upgrade. Older binaries reject v4. Indexed IDs enlarge
  FTS and may change BM25 scores/order, while text membership remains confined
  to titles/content. See [upgrade and rollback](docs/upgrading.md).
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
