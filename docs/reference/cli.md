# CLI reference

Operator commands write a JSON result. `serve` and `bridge` carry MCP protocol
traffic; `--help` and `--version` print text. A non-zero exit means
the requested action did not complete. Treat `partial`, `blocked`,
`verification_failed`, and `capture_partial` as work still requiring attention.
Use a private absolute data directory outside the checkout.

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
dots-brain verify --credential-file /absolute/private/credential.json --write --project work
```

Credential files are private capabilities. The CLI does not print them. `verify`
checks a real connection and can add a synthetic write only with `--write`.

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
```

Capture is a bounded import of supplied JSONL. `--recover-pending` only handles a
terminally quarantined pending receipt after inspection. CORTEX is optional and
uses a dedicated endpoint, token file, and explicit project mapping. The local
owner alone may resolve an uncertain, idempotent note for retry. Decisions and
outcomes are never automatically duplicated.
