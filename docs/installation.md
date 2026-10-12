# Installation and operation

This version uses schema v2. Use an identified reviewed commit or
its verified wheel. Linux is the managed deployment target. Python 3.11 or newer
must provide SQLite 3.42 or newer with FTS5. The retained CI matrix names Python
3.11 and 3.12; a local check does not establish both jobs ran. Data, backups and
exports need a local POSIX filesystem with SQLite and advisory lock support.
macOS supports local development and stdio checks; managed `up` requires Linux
process identity and pidfd support. Windows is unsupported.

## Initialize and connect

Choose the canonical private data directory outside the checkout:

```sh
uv sync --locked
uv run dots-brain --data-dir /absolute/private/memory setup
uv run dots-brain --data-dir /absolute/private/memory doctor
```

An existing v1 store needs the explicit [offline upgrade](upgrading.md). A new
store uses schema v2. Schema v3/v4 is refused; it is never relabeled or downgraded.

One client can use `serve` for owner stdio MCP. For a managed Linux HTTP service:

```sh
uv run dots-brain --data-dir /absolute/private/memory up
uv run dots-brain --data-dir /absolute/private/memory connect codex --project work
```

`up` checks a real read before publishing service metadata. Its probe credential
has read scope only. `connect` verifies a read through the generated bridge and
returns `write: "not_tested"`; it creates no synthetic memory. Reload the actual
client, then verify intended read/write calls separately. Client activation and
conversation behavior are not established by configuration.

`python scripts/bootstrap.py --data-dir /absolute/private/memory` runs the same
locked setup on Linux. Optional `--semantic` prepares the model; optional
`--connect PROVIDER --config PATH` configures a selected adapter. `mcp-json`
requires its explicit path. Failures return one sanitized JSON result naming
the failed phase (`prerequisites`, `arguments`, `dependencies`, `setup`, `model`,
`service` or `client`). Completed earlier phases are not rolled back; inspect the
named phase before retrying. Child output is not copied into that result.

## An existing supervisor

Run exactly one HTTP process for a store. A systemd or other supervisor can use:

```sh
dots-brain --data-dir /absolute/private/memory serve --transport http --port 8765 --public-port 8787
```

The private port accepts scoped static credentials and OAuth. The second port
accepts OAuth only; route public HTTPS solely to that port. See [OAuth](oauth.md)
for offline configuration and the explicit owner approval flow.

Use an existing private credential for read-only diagnosis. A new local client
can obtain its own scoped credential through `connect` with the private URL:

```sh
dots-brain --data-dir /absolute/private/memory doctor --mcp-url http://127.0.0.1:8765/mcp --credential-file /absolute/private/credential.json
dots-brain --data-dir /absolute/private/memory connect codex --project work --mcp-url http://127.0.0.1:8765/mcp
```

This bridge has no automatic `up`. Unreachable or mismatched endpoints fail
before successful client configuration. A failed connect restores its owned
changes where safe and preserves concurrent edits. If rollback cannot safely
remove a published configuration, it reports incomplete cleanup and retains
its recorded credential, integration and any newly started service. Inspect
that configuration before reconnecting or disconnecting; the error does not
mean that all earlier changes were reversed. `doctor` reports database,
OAuth and MCP checks separately; a remote issuer without a verified public
listener produces a warning. It does not scan or signal supervisor processes.
`up` refuses an already held HTTP lease; `down` leaves external services running.

## Optional local embeddings

```sh
uv sync --locked --extra semantic
uv run dots-brain --data-dir /absolute/private/memory model prepare
uv run dots-brain --data-dir /absolute/private/memory index
```

Preparation explicitly downloads the pinned local CPU model and verifies its
files. Enable semantic service operation with `--semantic` through the chosen
supervisor, or a separately authorized `down`/`up --semantic` cycle. `index`
reports `indexed_now` separately from the total. FTS remains available during
indexing or a query embedding failure. Relevance, especially short words and
negative queries, needs a corpus check and owner judgment; see [using memory](using-memory.md).

## Installation boundaries

The plugin manifest describes metadata and the setup skill. It does not install
a service or grant memory access. Capture and CORTEX are disabled and excluded
from the core distribution; their old configuration does not activate them.
See [activity](activity.md) for preserved state and activation boundaries. Boot startup, public
TLS, real-assistant behavior and recovery of real data need separate acceptance.
The deployment templates are examples, not an observed installation.
