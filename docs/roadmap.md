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

## VM and remote connection milestone

- Verify the target Dot VM's persistence, resource limits, and process lifecycle.
- Establish a stable authenticated HTTPS endpoint without additional fees.
- Verify one actual external client's read, write, restart, and revocation path.
- Verify the OAuth flow in actual web providers and isolate credential operations
  where the host allows it.

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

This migration is not implemented or approved for a live store. First rebind the
actual VM installation and its recovery path. The first cross-client acceptance
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
