# Installation and operation

This alpha runs on Python 3.11+ on a supported local host. Linux with Python 3.12
is the environment exercised during development. Do not infer Dot VM, macOS,
Windows, or individual AI-client certification from that result.

## Source installation

From a writable checkout, use `uv sync --frozen`. To include local semantic search,
use `uv sync --frozen --extra semantic`. For deterministic agent setup:

```sh
python scripts/bootstrap.py --data-dir /absolute/path/to/personal-memory --semantic
```

Replace the data directory with a real private path on the memory host. Do not
place it inside a shared repository. The script installs dependencies and prepares
the database and model. It does not configure a background supervisor or tunnel.

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
dots-brain --data-dir /absolute/path/to/personal-memory serve --transport http
```

The listener is `http://127.0.0.1:8765/mcp` and requires a credential on every request.
It is loopback-only and is not a public HTTPS deployment. Keep it behind the
current local boundary; public ingress and OAuth are not production-ready.

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
does not securely transfer credentials between machines or automatically modify
Claude, Cursor, or web account configuration.

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
use overlapping text chunks and a mean embedding; chunk-level citations and large
corpus performance are not established. No inference API is used as a fallback.

## Export, deletion, and recovery

`export --output <new-private-file>` exports live memories and all their revisions
as JSON Lines. It does not export credentials, deleted memories, or suppression
records. This export is not a complete disaster-recovery backup.

`memory_forget` removes a record, its revisions, search entries, and vectors, and
retains a hash of source identity to block accidental reimport. It does not erase
copies held by other assistants, old exports, or host backups. SQLite secure deletion
is enabled, but physical media erasure is not guaranteed.

Stop the service before taking a filesystem-level database backup and preserve
SQLite's journal state. A supported online backup/restore command and deletion-aware
restore policy are future work. A backup on the same disk cannot survive loss of that disk.
