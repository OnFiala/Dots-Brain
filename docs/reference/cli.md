# CLI reference

Operator commands write a JSON result. `serve` and `bridge` carry MCP protocol
traffic; `--help` and `--version` print text. Invalid command arguments exit 2.
Other failures exit 1 with `state: "error"` and a typed `code`; a successful
command exits 0 unless its result state is `partial`, `blocked`,
`verification_failed`, `capture_partial`, or `capture_recovery_required`. Those
states require operator action even when a result was written. Use a private
absolute data directory outside the checkout.

Callers should branch on `code`, not error prose. Common codes are
`invalid_input`, `revision_conflict`, `source_suppressed`, `migration_required`,
`capability_unavailable`, `busy`, `credential_rejected`, `service_unavailable`,
`timed_out`, and `internal_error`. A code describes the boundary result; it does
not make a remote service or client UI verified.

## Store and service

| Command | Purpose |
| --- | --- |
| `setup` | Initialize or reuse the local database. |
| `doctor` | Diagnose the database, OAuth configuration, and managed-service record. |
| `preflight [--network-policy PATH]` | Inspect host capabilities without changing state; only inspects a policy file explicitly supplied by the operator. |
| `serve` | Run one stdio or loopback HTTP MCP server. |
| `up` / `down` | Start or stop the managed loopback HTTP service. |
| `providers` | List implemented client adapters. |
| `connect` / `disconnect` | Add or remove a managed local client connection. |
| `uninstall` | Disable the instance and detach managed clients while retaining data. |

```sh
dots-brain --data-dir /absolute/private/memory setup
dots-brain --data-dir /absolute/private/memory doctor
dots-brain preflight
dots-brain preflight --network-policy /absolute/path/network-policy.json
dots-brain --data-dir /absolute/private/memory serve
dots-brain --data-dir /absolute/private/memory serve --transport http --port 8765
dots-brain --data-dir /absolute/private/memory up
dots-brain --data-dir /absolute/private/memory up --port 8766 --semantic
dots-brain --data-dir /absolute/private/memory down
dots-brain --data-dir /absolute/private/memory providers
dots-brain --data-dir /absolute/private/memory connect codex --project work
dots-brain --data-dir /absolute/private/memory disconnect codex --dry-run
dots-brain --data-dir /absolute/private/memory uninstall --dry-run
```

`up --resume` is the explicit way to re-enable an intentionally uninstalled
store. `connect` verifies the generated bridge, then the actual client must be
reloaded and used for a real MCP read.

## Data, migration, and recovery

`migrate --apply` and cutover commands require every writer to be stopped. The
flag is an operator declaration and an exclusive writer lease checks cooperating
servers. The command cannot stop clients or supervisors for you, or protect
against direct SQLite writers outside Dots Brain. Restore always stages a disabled target. Keep the
surviving source store until the target has passed its offline checks.

```sh
dots-brain --data-dir /absolute/private/memory migrate
dots-brain --data-dir /absolute/private/memory migrate --apply --writers-stopped --backup /absolute/private/backup.sqlite3
dots-brain --data-dir /absolute/private/memory backup --output /absolute/private/backup.sqlite3
dots-brain --data-dir /absolute/private/memory restore --backup /absolute/private/backup.sqlite3 --target /absolute/private/restored
dots-brain --data-dir /absolute/private/memory activate-restore --target /absolute/private/restored --writers-stopped
dots-brain --data-dir /absolute/private/memory abort-restore --target /absolute/private/restored --writers-stopped
dots-brain --data-dir /absolute/private/memory export --output /absolute/private/export.jsonl
```

`abort-restore` only cancels the exact pending cutover. It cannot undo a committed
cutover. Current restore needs the surviving source for deletion reconciliation;
it is not a host-loss recovery feature.

## Semantic index

```sh
dots-brain --data-dir /absolute/private/memory model prepare
dots-brain --data-dir /absolute/private/memory index
dots-brain --data-dir /absolute/private/memory index --retry-failed
```

`model prepare` downloads the pinned local model. Index state can be `indexed`,
`empty`, or `failed`; a record may also be marked `truncated` when bounded chunk
processing reaches its limit. Full-text search continues to work without it.

## Credentials and local bridge

```sh
dots-brain --data-dir /absolute/private/memory client create --name local-tool --scope memory:read --project work --credential-file /absolute/private/credential.json --url http://127.0.0.1:8765/mcp
dots-brain --data-dir /absolute/private/memory client list
dots-brain --data-dir /absolute/private/memory client revoke client-id
dots-brain bridge --credential-file /absolute/private/credential.json
dots-brain verify --credential-file /absolute/private/credential.json --project work
```

Credential files are private capabilities. The CLI does not print them. `verify`
checks a real read connection by default. It creates and then removes a synthetic
probe only with `--write`. That probe needs `memory:read`, `memory:write`, and
`memory:forget` on the selected project so it can verify and remove its record.
`client create` defaults to `memory:read` and a 30-day credential lifetime;
use explicit scopes and `--days` (up to 365) for another grant. Expired or revoked
credentials are rejected rather than renewed.

## OAuth

```sh
dots-brain --data-dir /absolute/private/memory oauth configure --issuer https://memory.example
dots-brain --data-dir /absolute/private/memory oauth onboarding open --minutes 10
dots-brain --data-dir /absolute/private/memory oauth pending
dots-brain --data-dir /absolute/private/memory oauth approve request-id --redirect-host client.example --project work --scope memory:read
dots-brain --data-dir /absolute/private/memory oauth grants
dots-brain --data-dir /absolute/private/memory oauth revoke grant-id
dots-brain --data-dir /absolute/private/memory oauth onboarding close
dots-brain --data-dir /absolute/private/memory oauth disable
```

Changing issuer needs `oauth configure --replace-issuer` and revokes old OAuth
grants. The CLI onboarding TTL and the public gateway gate are separate controls.
See [OAuth](../oauth.md) for the approval procedure and request-size limits.

## Audit, capture, and optional CORTEX

```sh
dots-brain --data-dir /absolute/private/memory audit events --project work --after-id 0 --limit 100
dots-brain --data-dir /absolute/private/memory audit report --project work
dots-brain capture --kind audit --path /absolute/private/snapshot.jsonl --cursor /absolute/private/cursor.json --credential-file /absolute/private/credential.json --project work --account account-id
dots-brain --data-dir /absolute/private/memory cortex configure --endpoint https://cortex.example/mcp --token-file /absolute/private/cortex-token --project-map work=work
dots-brain --data-dir /absolute/private/memory cortex operations --project work
dots-brain --data-dir /absolute/private/memory cortex resolve --project work --operation-id operation-id --resolution retry
dots-brain --data-dir /absolute/private/memory cortex resolve --project work --operation-id operation-id --resolution recover-sending --writers-stopped
```

Capture is a bounded import of supplied JSONL. `--recover-pending` only handles a
terminally quarantined pending receipt after inspection. CORTEX is optional and
uses a dedicated endpoint, token file, and explicit project mapping. The local
owner may explicitly retry an uncertain note, decision, or outcome after review;
decisions and outcomes have no upstream idempotency key and a retry can duplicate
them. `recover-sending` never retries: it marks a record left in `sending` by a
terminated sender as uncertain. It requires every cooperating writer to be stopped
and an exclusive writer lease; then reconcile or make a separate explicit retry decision.

Capture accepts at most 1,000 records or 16 MiB in one pass, and rejects lines
over 256 KiB. Its cursor is durable. If replacement, truncation, or rewrite changes
an acknowledged prefix, the collector records a gap and blocks that source under
the current cursor; it never falls back to reimporting acknowledged rows. See
[activity and capture](../activity.md) for the supported snapshot shapes.

### Private audit review helper

`scripts/audit_review.py` is a separate operator helper, not an MCP tool and not a
deployment service. It fetches only the fixed `dots-brain audit events` command
over SSH, adaptively reduces a page that exceeds its 1 MiB response budget, and
keeps bounded checkpoints and findings in its private state directory.

Create `target.json` in that state directory, or pass it with `--target-config`:

```json
{
  "origin": "operator-audit",
  "host": "audit-host.example",
  "user": "dotsbrain",
  "executable": "/absolute/path/to/dots-brain",
  "data_dir": "/absolute/private/memory",
  "timezone": "UTC"
}
```

Keep this file private: it contains host and filesystem layout. The helper accepts
only these validated fields, not a shell command. Run `scan` with a reviewed
run key, review its output, then use `complete` with the matching review file.
When `complete` returns `has_more: true`, run the next chunk with the same run key;
the helper preserves the continuation checkpoint only after a valid reviewed chunk.
