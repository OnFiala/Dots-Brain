# Roadmap

## First alpha — implemented

- Source-aware memory storage, full-text and local semantic retrieval, and deletion.
- Six MCP tools, scoped loopback HTTP, stdio, and a bridge to the shared service.
- Local setup, diagnostics, agent instructions, and reproducible MIT-licensed builds.

See the capability matrix and verification evidence for the exact supported scope.

## Local automation alpha — implemented

- One-command bootstrap, shared background startup, and recovery on local reconnect.
- Claude Code, Cursor, Codex, and generic MCP configuration adapters.
- Verified generated bridges, private credential reuse, and preserved client settings.
- Synthetic stress tests, responsive reads during blocked writes, and faster backlog indexing.

## Lifecycle and OAuth alpha — implemented

- Retained-data uninstall, individual client disconnection, and explicit reinstall.
- English installation, usage, upgrade, troubleshooting, and removal guides.
- VM-local OAuth using the official MCP SDK, with agent-operated project approval.
- PKCE, resource binding, hashed bearer tokens, rotating refresh, and revocation.
- Official OAuth client verification against a live local HTTP memory service.

## Appliance and remote connection milestone

- Deploy one canonical instance on the selected owner-operated Linux appliance.
- Verify persistence, resource limits, supervision, backup, restore, and host-loss handling.
- Establish a stable authenticated HTTPS endpoint using the authorized infrastructure.
- Verify Botter and Grok Bot's actual read, write, restart, and revocation paths.
- Verify the OAuth flow in actual web providers and isolate credential operations
  where the host allows it.

The [appliance contract](appliance-contract.md) is the current target. Historical
Dot VM recovery is independent and does not block a fresh, clearly identified
instance or the bots' initial knowledge contributions.

## CORTEX connectors and bot contributions — required next delivery

- Add separate scoped CORTEX read and selected write capabilities with original
  references, durable receipts, no implicit permission expansion, and safe handling
  of uncertain writes. Keep local memory available during CORTEX outages.
- Support initial contributions of the knowledge each bot can actually access,
  with stable identities, self-report/source distinctions, conflict visibility,
  and resumable verification. Do not present this as complete history import.
- Install and verify each bot's routine of context lookup, sourced new writes,
  and corrections across sessions. A connected client is not proof of actual use.
- Treat any cloud extraction helper as optional follow-up; no model API is needed
  for initial direct contributions or ordinary explicit memory writes.

## Shared-client data boundaries — next proposed migration

The unreleased code requires observed revisions for deletion and validates the
store/credential/endpoint before local resume. It still uses schema version 1.
Before treating projects as independent write namespaces, prepare and test an
explicit offline schema migration with these constraints:

- Include the project in source identity and new deletion suppression keys.
- Retain existing v1 suppression hashes unchanged as global legacy tombstones;
  their original projects cannot be recovered from the hashes. Never drop them
  to make an import succeed.
- Record a server-derived writer principal per new revision and deletion. Keep
  source metadata client-declared. Both authorized bots may update the same record
  within one project; writer attribution does not grant exclusive ownership.
- Preserve identical retries as no-ops without changing the original writer.
  Mark historical authorship as unknown rather than inventing a principal.
- Require stopped clients and services, a private verified pre-migration backup,
  one atomic migration, and rejection by older binaries, including direct stdio.
  Merely changing SQLite's version number does not stop an already running old
  server from using its old SQL.
- Test preservation of history, indexes, credentials and OAuth grants; legacy
  versus scoped suppression; failure rollback; repeated migration; and old-binary
  rejection. Returning to the old backup after new writes or deletions requires
  explicit reconciliation and must not be presented as lossless rollback.

This migration is not implemented or approved for a live store. Preserve it as the
upgrade path for any recovered v1 store; a fresh appliance store does not require
recovering the old VM first. Shared-writer fixes still precede personal bot imports.
The first cross-client acceptance
milestone is one bot saving a synthetic note and the other retrieving the same
record/revision from the identified store, followed by credential revocation and
authorized cleanup. Automatic conversation capture is a later milestone.

## Product milestone

- Extend the initial multilingual smoke test to a fixed bilingual evaluation set.
- Add provider-specific capture adapters without changing the memory core.
- Add tested native plugin views, onboarding, and MCP Events.
- Verify clean installation, upgrades, export, recovery, and multi-client access.

Development starts now. Production readiness requires completing the relevant
verification milestones, not merely assigning a release number.
