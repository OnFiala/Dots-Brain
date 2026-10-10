# Installation and operation

## Supported environment

Managed deployment is supported on Linux with Python 3.11, 3.12, or 3.13 and
SQLite 3.42 or newer. SQLite must support FTS secure deletion. Older SQLite
builds are not a supported upgrade or rollback path for a schema-v3 store.
macOS can run local source checks and stdio experiments, but managed lifecycle
behavior is not accepted there. Windows is not supported.

Use a writable checkout and `uv`; this alpha has no supported `pip install` path.
Put the data directory and every backup or export destination on a local POSIX
filesystem that supports hard links. Dots Brain refuses atomic publication on an
unsupported destination with `capability_unavailable`; use a local directory rather
than weakening the publication step.

```sh
uv sync --locked
uv run dots-brain --data-dir /absolute/private/memory setup
uv run dots-brain --data-dir /absolute/private/memory doctor
```

Choose a private directory outside the checkout. `setup` initializes or reuses the
selected current-schema store. Older stores require the explicit
[offline upgrade](upgrading.md). `doctor` reports local state and does not prove public ingress,
another device, or client UI activation.

## Local MCP

For one local client, run a stdio server:

```sh
uv run dots-brain --data-dir /absolute/private/memory serve
```

For several local clients, start the managed loopback HTTP service and configure a
supported adapter on the same host:

```sh
uv run dots-brain --data-dir /absolute/private/memory up
uv run dots-brain --data-dir /absolute/private/memory connect codex
```

`up` returns the selected loopback endpoint. It does not provision a public route.
`connect` verifies its generated bridge with a synthetic probe; reload the actual
client and make a real MCP read before claiming conversation-level activation.

## Local semantic search

The default installation uses full-text search. To prepare local CPU embeddings:

```sh
uv sync --locked --extra semantic
uv run dots-brain --data-dir /absolute/private/memory model prepare
uv run dots-brain --data-dir /absolute/private/memory down
uv run dots-brain --data-dir /absolute/private/memory up --semantic
```

`model prepare` downloads the pinned, integrity-checked multilingual MiniLM
artifact for local CPU use: about 241 MiB for the model and tokenizer files.
Runtime memory is higher than download size and depends on workload.
Full-text search remains available while semantic
indexing catches up. Run `index --retry-failed` after fixing a failed model or
environment. There is no paid API fallback. Semantic work is bounded to at most
128 chunks per memory and 16 query chunks, so unusually large records can have
incomplete semantic coverage while full-text search remains available.

## Boundaries

Remote OAuth needs an already-operated HTTPS route and explicit owner approval;
it is documented in [OAuth](oauth.md). The bootstrap script is an explicit install
or reinstall action. It can resume an intentionally disabled instance, so use
`doctor` for inspection alone.

An optional CORTEX connection requires its own endpoint, credential file, project
map, and live verification. It is neither configured nor verified by installation.
See [autonomy](autonomy.md) and [troubleshooting](troubleshooting.md).

## Codex plugin package

`plugin.json` describes this repository's Codex package: its display metadata and
the onboarding skill at `skills/setup/SKILL.md`. It does not install a service,
create a store, grant access to data, or verify an MCP connection. Run the setup
and verification commands above on the host that will own the store.
