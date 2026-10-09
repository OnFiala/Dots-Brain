# Installation and operation

This alpha runs on Python 3.11+ on a supported local host. The current internal
deployment is Linux/Python 3.12.3 on `openclaw-appliance`; see its
[installation and acceptance](appliance-deployment.md). The earlier shared
Work/Dot VM is [historical evidence](vm-deployment.md), not the current service.
These checks do not certify every host or individual AI client. The public route and both named bots are verified. A real appliance reboot and
host-loss recovery remain unverified.

## Source installation

From a writable checkout, use `uv sync --frozen`. To include local semantic search,
use `uv sync --frozen --extra semantic`. For deterministic agent setup:

```sh
python scripts/bootstrap.py --data-dir /absolute/path/to/personal-memory --semantic
```

Replace the data directory with a real private path on the memory host. Do not
place it inside a shared repository. The script installs dependencies and prepares
the database and model, starts the local background service, and verifies MCP
readiness. Add `--connect claude-code`, `--connect cursor`, or `--connect codex`
for automatic client configuration. See [autonomous setup](autonomy.md).
It does not configure a persistent supervisor or tunnel.

For later lifecycle operations, see [upgrading](upgrading.md),
[uninstall and reinstall](uninstall.md), and [troubleshooting](troubleshooting.md).
The bootstrap is an explicit install/reinstall action and re-enables an instance
previously disabled by `uninstall`. Use `doctor` for inspection without resuming it.

## Basic commands

The following commands assume the installed executable is on PATH. In a source
checkout, use `.venv/bin/dots-brain` on Linux. `--data-dir` precedes the subcommand.

```sh
dots-brain --data-dir /absolute/path/to/personal-memory setup
dots-brain --data-dir /absolute/path/to/personal-memory doctor
dots-brain --data-dir /absolute/path/to/personal-memory serve
```

The default `serve` transport is stdio. It runs as the local owner and should only
be launched on the machine holding the canonical store. Stdout is MCP protocol
traffic, not a human-readable log. Exit the client to stop its stdio process.

For multiple clients on the same host, run one service:

```sh
dots-brain --data-dir /absolute/path/to/personal-memory up
```

The default listener is `http://127.0.0.1:8765/mcp`; startup selects an available
port if the default is occupied on first installation. The JSON result contains
the actual endpoint. Every request requires a credential.
This command creates a loopback service. It does not provision public ingress.
The appliance uses a separately configured restricted HTTPS route with
[OAuth](oauth.md); both Botter and Grok Bot have verified actual chat access.
Follow the [ingress guide](appliance-ingress.md) when deploying a remote route.

## Scoped client connection

Create a private destination directory first. The following command writes a
credential directly to a new file, without printing its value:

```sh
dots-brain --data-dir /absolute/path/to/personal-memory client create \
  --name local-assistant --project my-project \
  --scope memory:read --scope memory:write \
  --credential-file /absolute/private/path/client.json

dots-brain verify --credential-file /absolute/private/path/client.json
```

Credentials expire after 30 days by default. `--days` accepts 1–365. Omitting
`--scope` grants read only; omitting `--project` grants the chosen scopes across
all projects. Deletion requires the separate `memory:forget` scope. Do not grant
it implicitly. `client revoke <client-id>` invalidates subsequent HTTP requests.

`verify` tests discovery and a real status read. It does not certify writes,
capture, complete history, a specific vendor client, or restart persistence.

A stdio-only client can use the bridge, which holds no separate database:

```sh
dots-brain bridge --credential-file /absolute/private/path/client.json
```

Configure the actual executable and arguments through that client's supported
installation interface. The path must exist on the client machine. This alpha
does not securely transfer credentials between machines or modify web account
configuration. `connect` automates supported client configuration on the current
machine and verifies the generated bridge before saving it.

The credential file is intentionally private but readable by its OS owner. It is
not an isolated vault. Do not open it in chat, copy it into a plugin, or commit it.

## Local semantic search

Install the semantic extra, then prepare the pinned model explicitly:

```sh
dots-brain --data-dir /absolute/path/to/personal-memory model prepare
dots-brain --data-dir /absolute/path/to/personal-memory index
dots-brain --data-dir /absolute/path/to/personal-memory serve --transport http --semantic
```

Only `model prepare` downloads model files. Model loading verifies pinned hashes;
inference runs locally on CPU with two ONNX threads. The enabled server indexes
pending records in small background batches. Pending records remain available to
full-text search, and `memory_status` reports the semantic backlog.

The initial model is Apache-2.0 licensed, with approximately 241 MiB of downloaded
artifacts. See [verification](verification.md) for measured memory use. Long records
use overlapping 96-token windows with an 80-token stride, within the model
128-token input limit. Title and content passages have separate vectors; results
include the matched field and offsets. Large-corpus capacity remains unmeasured
on the appliance. No inference API is used as a fallback.

## Export, deletion, and recovery

`export --output <new-private-file>` exports live memories and all their revisions
as JSON Lines. It does not export credentials, deleted memories, or suppression
records. This export is not a complete disaster-recovery backup.

`memory_forget` removes a record, its revisions, search entries, and vectors, and
retains a hash of source identity to block accidental reimport. It does not erase
copies held by other assistants, old exports, or host backups. SQLite secure deletion
is enabled, but physical media erasure is not guaranteed.

Use `backup --output <new-private-file>` for a validated SQLite snapshot including
WAL. Use `restore` followed by explicit offline `activate-restore` as documented in
[upgrading and recovery](upgrading.md). A backup on the same disk cannot survive
loss of that disk. Scheduled off-host backups remain a deployment requirement.
