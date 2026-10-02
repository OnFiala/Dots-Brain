# Autonomous setup contract

The user should be able to ask an agent to connect Dots Brain once. The agent
runs the shipped installer and verifies the result. It must not generate a new
server, ask the user to copy tokens, or describe a saved configuration as a
working application connection.

## Implemented path

On the authorized Linux memory host:

```sh
python scripts/bootstrap.py --data-dir /absolute/private/memory --connect claude-code
```

Add `--semantic` to install and prepare local embeddings. The bootstrap installs
locked dependencies, reuses the database, starts an authenticated background
service, and connects the selected client. Repeating it reuses the service and
credential. Concurrent setup calls converge on one process. An expired or
revoked installer-managed credential can be replaced by the authorized host owner.

For an existing installation:

```sh
dots-brain --data-dir /absolute/private/memory up
dots-brain --data-dir /absolute/private/memory connect cursor
dots-brain --data-dir /absolute/private/memory connect codex
dots-brain --data-dir /absolute/private/memory providers
```

All three named adapters configure a stdio bridge to the same authenticated HTTP
service. The configured bridge is launched and tested before saving settings.
Local connection checks write, read, and remove a synthetic probe. Credentials
stay in private files; client configuration contains only executable arguments
and file paths. Existing unrelated configuration is preserved and backed up.
A conflicting Dots Brain entry is never overwritten silently.

The bridge starts a stopped local service when invoked. This provides recovery
when a local client reconnects; it is not continuous process supervision or
automatic startup after a VM reboot. `down` stops only the recorded process,
checks its identity to avoid a reused PID, and preserves the database.

## Provider adapters

| Adapter | Default configuration | Evidence |
| --- | --- | --- |
| `claude-code` | `~/.claude.json` or the Claude configuration directory | Real Claude Code 2.1.287 reported Connected in an isolated test profile |
| `cursor` | `~/.cursor/mcp.json` | Configuration preservation and actual SDK bridge calls tested; Cursor UI not tested |
| `codex` | `~/.codex/config.toml` | TOML comments/settings preserved; actual SDK bridge calls tested; Codex UI not tested |
| `mcp-json` | Explicit `--config` path | Generic `mcpServers` configuration; actual SDK bridge calls tested |

`--config` selects the actual file on the current device. New provider adapters
belong in the adapter registry and reuse the same verification path. An unknown
provider returns a concrete blocked result without creating another database.
Automatic capture and history import are separate features and remain unimplemented.

## Remote devices and web clients

An agent running only on a VM cannot configure a laptop it cannot access. If the
agent has an authorized execution path to that device, it can run the installer
there. Otherwise a device connection is a real missing capability, not a reason
to create a second memory store. A previously provisioned credential can be used
with `connect <provider> --credential-file <private-file>` on the client machine;
this path never initializes another database and verifies read access only.

The current release does not provide public ingress or web OAuth. `preflight`
reports the host's supported network-policy snapshot and whether TCP destinations
are configured. The inspected managed development VM allows HTTP/HTTPS through
its proxy and has no configured TCP destinations, VPN, or systemd runtime.
This does not establish support for Cloudflare Tunnel, Tailscale, a public URL,
or durable service supervision. The installer never changes platform policy.

A complete web onboarding path requires the platform to supply a stable public
route or a permitted tunnel transport, plus a supported client registration and
OAuth consent flow. Those dependencies cannot be removed by generating a token.
Reuse existing authorized connections and never ask for the same consent twice.
Do not suppress an account's mandatory authentication or device trust checks.

## Machine-readable completion

Report `read`, `write`, `application_activation`, `capture`, `memory_host`, and
`secret_isolation` separately. `configured_verified_bridge` confirms the generated
bridge and its granted memory operations; it does not assert that the application's
current conversation loaded the new configuration. An application may need to
reload or show its own first-use permission prompt.

Private credential files are readable by their OS owner. Strong secret isolation
still requires a platform or OS boundary. There is no paid API, hosted database,
external authentication service, or operator-run relay in this implementation.
