# Capability matrix

This describes the unreleased `0.4.0-alpha.1` candidate. The appliance and public
HTTPS/OAuth route passed synthetic acceptance on 2026-10-09. Grok's actual chat
read/write connection is verified. Botter and cross-bot acceptance remain pending.

| Capability | Implemented evidence | Remaining boundary |
| --- | --- | --- |
| Project-scoped memory, revisions, deletion, authenticated writer | SQLite and actual MCP SDK tests | Existing v1 stores need offline migration |
| Full-text and bounded context | Tests for scoped retrieval and budgets | Character budgets are not token budgets |
| Local semantic passages | Pinned ONNX model; token-window, title, stale/deletion and real-model tests | Appliance capacity and larger bilingual evaluation |
| MCP stdio, loopback HTTP, local bridge/adapters | SDK, synthetic credentials, supported Linux process tests; Grok actual chat status/write/read | Botter client activation and cross-bot readback |
| OAuth registration, PKCE, refresh, revocation | Official SDK flow, scoped handler tests, public synthetic acceptance and real Grok grant | Botter fresh registration after read-only DCR mismatch; durable request-to-grant provenance |
| Sanitized action audit | Append-only rows, server mutation intent/receipt, client reports, gaps and pagination | Full provider audit cannot be inferred from submitted events |
| Bounded JSONL snapshot collector | ACK retry, partial lines, rotation, oversized records, missing sources, crash journal | Live provider logs and persistent collectors unverified |
| Transcript import | User text with stable file-record identity; tool metadata only | Ambiguous assistant text excluded; no transcript timestamps/IDs invented |
| CORTEX context and selected writes | Project mapping, source revision, scope checks, durable receipts and uncertain-write tests | Dedicated authorized upstream endpoint and live behavior |
| Schema v1→v2 migration | Offline backup, transactional rollback, preserved history/credentials and legacy barriers | No migration performed on personal data |
| Backup and recovery | SQLite backup with validation; disabled restore; latest deletions, revoked auth and divergence-safe cutover | Scheduled off-host backups and host-loss recovery |
| Appliance supervision | Installed active/enabled systemd service, loopback listener, verified restart and retained IDs | Actual machine reboot and long-run capacity untested |
| Initial bot contributions and later recall | Stable record/revision API and receipt protocol available | Both bots must actually connect, seed, and verify later-session use |
| Twice-daily Codex audit analysis | Active 09:00/21:00 heartbeat, manual baseline and durable review helper; first scheduled attempt observed | First attempt could not resolve the tailnet host in its restricted execution context; no cursor advancement. Backend model identity and full provider capture unverified |
| Cerebras helper | Optional architecture proposal | No API key, call, charge or runtime dependency |
| Native ChatGPT views, MCP Events | Planned | No client extension shipped |

The old Work/Dot VM was historically verified on 2026-10-05. Its installation paths
were absent in the owner's 2026-10-09 checks. See [historical evidence](vm-deployment.md).
The selected new host is `openclaw-appliance`; a fresh instance is not recovery of
that old database. Credential files are private to their OS owner, not an isolated vault.
