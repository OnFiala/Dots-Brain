# Changelog

## 0.4.0-alpha.2 — Unreleased

This candidate has not been tagged, published, or deployed by this repository
state.

### Added

- Add CLI and MCP reference guides, plus a generic Linux deployment template.
- Add local CORTEX operation inspection and owner-authorized retry for idempotent
  uncertain notes.

### Changed

- Require SQLite 3.42 or newer for secure FTS deletion behavior. Existing schema
  v2 stores use the explicit v1-to-v2 migration path.
- Make backup creation standalone and atomic, and preserve a safe recovery state
  when restore cutover validation fails.

### Fixed

- Add a partial abort-recovery command for a failed cutover. It requires the
  documented offline operator checks and cancels only a pending cutover.
- Replace owner-specific deployment records with public Linux installation and
  ingress guidance.

### Breaking

- Existing stores backed by unsupported SQLite versions cannot be safely upgraded
  or rolled back through this candidate.

## 0.3.0-alpha.2 — 2026-10-04

- Add optional local OAuth configuration with PKCE, scoped owner approval, refresh,
  and revocation.
- Add repeatable local service startup, client adapters, and a verified bridge.

### Known limits

- Public ingress, provider account setup, continuous conversation capture, native
  client UI activation, off-host recovery, and reboot acceptance are not verified.
