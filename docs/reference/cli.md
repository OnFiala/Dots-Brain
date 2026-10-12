# CLI reference

Use `dots-brain --data-dir /absolute/private/memory COMMAND --help` for the exact
v2 options. Operator results are JSON; `serve` and `bridge` carry MCP protocol
traffic. Argument errors exit 2. Runtime errors exit 1 with `state: "error"` and
a safe `code`. `partial`, `blocked` and `verification_failed` also exit 1. Inspect
individual checks and warnings even when a command returns zero.

Current typed codes include `invalid_input`, `not_found`, `revision_conflict`,
`source_suppressed`, `capability_unavailable`, `migration_required`,
`store_disabled`, `invalid_arguments`, `timeout`, `connection_failed`, `permission_denied`, `busy`,
`database_error` and the safe boundary fallback `operation_failed`.

| Command | Current purpose |
| --- | --- |
| `setup`, `doctor` | Initialize/reuse the store and inspect independent checks. |
| `serve [--transport http --port 8765 --public-port 8787]` | Owner stdio or one process with private and OAuth-only HTTP listeners. |
| `up [--port PORT] [--semantic] [--resume]`, `down` | Linux managed lifecycle; explicit resume only after ordinary uninstall. |
| `providers` | Adapter capabilities and remaining client activation boundary. |
| `connect PROVIDER [--config PATH] [--project PROJECT] [--mcp-url URL]` | Verify a read and configure a bridge; explicit private URL uses the existing supervisor. |
| `connect PROVIDER --credential-file PATH` | Configure using an existing supplied connection; no second local store. |
| `disconnect PROVIDER [--config PATH] [--dry-run]` | Remove a recorded connection within its current ownership rules. |
| `uninstall [--dry-run] [--config PROVIDER=PATH]` | Offline disable/revoke/detach; preserve data. |
| `migrate [--apply --writers-stopped] [--backup PATH]` | Plan/apply the released v1 to v2 migration. |
| `backup --output PATH` | Consistent standalone private snapshot. |
| `restore --backup PATH --target PATH` | New disabled target reconciled with the surviving source's deletion barriers. |
| `activate-restore --target PATH --writers-stopped` | Validate and activate the exact staged recovery. |
| `model prepare`, `index` | Prepare the optional pinned model; index pending memories and report `indexed_now`. |
| `export --output PATH` | Private JSONL export. |
| `client create`, `client list`, `client revoke ID` | Manage scoped static credentials. |
| `bridge --credential-file PATH` | Per-call remote MCP sessions; optional managed `--local-data-dir` only on the memory host. |
| `verify --credential-file PATH [--write --project PROJECT]` | Read by default; explicit write mode uses a synthetic write/delete probe. |
| `oauth configure`, `disable`, `status`, `pending`, `approve`, `deny`, `grants`, `revoke` | [OAuth](../oauth.md) configuration and approval lifecycle. |
| `audit report`, `audit events` | Existing v2 operation metadata with bounded pages. |
| `capture`, `cortex` | Existing optional integration surfaces, not activated or accepted by this batch. |

For supervisor diagnosis:

```sh
dots-brain --data-dir /absolute/private/memory doctor --mcp-url http://127.0.0.1:8765/mcp --credential-file /absolute/private/credential.json
```

`doctor` never creates a probe grant or starts a service. `connect` returns
`write: "not_tested"` and needs actual client reload/usage afterward. Without an
explicit URL, local `connect` uses managed `up` on Linux. `--mcp-url` and a supplied
`--credential-file` are alternative connect modes.

Bulk OAuth changes, uninstall, migration and recovery require stopped supported
writers. Offline flags do not terminate supervisors and cannot protect against
uncooperative direct SQLite writers. Preserve recovery state and follow
[upgrading](../upgrading.md). See [disconnect and uninstall](../uninstall.md) for
ownership, retained-file and remote-revocation outcomes, and [activity](../activity.md)
for review paging.
