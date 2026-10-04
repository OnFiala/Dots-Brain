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

## Product milestone

- Extend the initial multilingual smoke test to a fixed bilingual evaluation set.
- Add provider-specific capture adapters without changing the memory core.
- Add tested native plugin views, onboarding, and MCP Events.
- Verify clean installation, upgrades, export, recovery, and multi-client access.

Development starts now. Production readiness requires completing the relevant
verification milestones, not merely assigning a release number.
