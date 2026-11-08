# Dots Brain

Dots Brain is a self-hosted MCP memory service for explicitly saved notes. It
stores source metadata and revisions in one SQLite database, supports full-text
search, and can add local CPU embeddings. It does not automatically collect
assistant conversations.

Version 1.0.1 is the stable release of the Linux core: explicit notes,
source references, revisions, full-text search and optional local CPU embeddings.
Publication and acceptance on your own host remain separate steps.

A **Dot** means an AI assistant or client using this shared service. It is a
product term, not a separate database or a grant of access.

**Source version: 1.0.1.** The core includes schema-v2 storage, scoped access,
MCP tools and offline maintenance. Capture and CORTEX modules are disabled and
excluded from the core distribution. Stability applies to the documented core;
it does not guarantee that every assistant uses memory in every conversation.

## Quick start on Linux

Use Python **3.11 or 3.12** with SQLite **3.42 or newer and FTS5**,
[uv](https://docs.astral.sh/uv/), `curl`, `unzip` and `sha256sum`.
Install the versioned source/plugin ZIP from the
[1.0.1 release](https://github.com/OnFiala/Dots-Brain/releases/tag/v1.0.1).
Start in a new installation directory, then verify the archive before extracting it:

```sh
set -eu
RELEASE_URL=https://github.com/OnFiala/Dots-Brain/releases/download/v1.0.1
curl --fail --location --output SHA256SUMS "$RELEASE_URL/SHA256SUMS"
curl --fail --location --output dots-brain-1.0.1-plugin.zip "$RELEASE_URL/dots-brain-1.0.1-plugin.zip"
sha256sum --check --ignore-missing SHA256SUMS
unzip dots-brain-1.0.1-plugin.zip
cd dots-brain
uv sync --locked --no-dev --python 3.12
.venv/bin/dots-brain --version
.venv/bin/dots-brain --data-dir /absolute/private/memory setup
.venv/bin/dots-brain --data-dir /absolute/private/memory doctor
```

The version must be `1.0.1`. Use `--python 3.11` instead if needed. The ZIP
contains the release's dependency lock; keep both together. See
[installation](docs/installation.md) for the verified wheel alternative and
prerequisite checks. Use a local POSIX filesystem with working SQLite and
advisory locks for data, backups and exports.

For several clients on the same Linux host, use `up` to run an authenticated
loopback HTTP service, then configure a supported local adapter. See
[installation](docs/installation.md).

```sh
.venv/bin/dots-brain --data-dir /absolute/private/memory up
.venv/bin/dots-brain --data-dir /absolute/private/memory connect codex --project work
```

Linux is the supported deployment platform. macOS is suitable for local source
work and stdio experiments; managed lifecycle behavior is not accepted there.
Windows is not supported. A connection configured by `connect` is verified at the
bridge boundary; it does not prove a remote device, web account, or conversation
has loaded the new tools.

After reloading the client, ask it to save a short project decision in `work`,
then ask it to find that decision. The expected calls are `memory_remember`,
`memory_search` or `memory_context`, and `memory_get` for the exact source.
Saving happens when the client calls a tool; connecting alone does not save a
conversation. See the [tool reference](docs/reference/mcp-tools.md).

| Adapter | Default client configuration |
| --- | --- |
| `codex` | `~/.codex/config.toml` |
| `claude-code` | `~/.claude.json`, or `CLAUDE_CONFIG_DIR` |
| `cursor` | `~/.cursor/mcp.json` |
| `mcp-json` | A path supplied with `--config` |

## Boundaries

- Data remains in the selected private directory. A checkout is not a data store.
- An MCP connection gives access only to explicitly saved memory. Snapshot capture
  is disabled and excluded from the core package.
- Remote OAuth needs an existing HTTPS route and an explicit owner approval flow.
  Dots Brain does not provision public ingress or configure web accounts.
  Authorization lasts 30 days; refreshing tokens does not extend it. Expiry
  requires owner-approved pairing again and does not delete stored notes.
- CORTEX is a separate service for project context, decisions and outcomes.
  Its connector is disabled and excluded from the core package. Existing module
  configuration and state are preserved; [activation needs a separate plan](docs/activity.md).
- Backups on the same disk do not recover a lost host. Restore requires a current
  store for deletion reconciliation; full host-loss recovery is not yet supported.

Read [security](SECURITY.md), [upgrading and recovery](docs/upgrading.md), and the
[documentation index](docs/README.md) before operating a long-lived store.

## Development

```sh
uv sync --locked --all-extras
uv run ruff check src tests scripts modules
uv run ruff format --check src tests scripts modules
uv run pytest -q
uv run python scripts/validate_project.py
uv build
```

Tests use disposable stores. The retained CI matrix covers Python 3.11 and 3.12. See
[verification](docs/verification.md) for the distinction between source checks
and deployment acceptance. Repository tests include preserved module characterizations
under `modules/tests`; the core source distribution contains only core tests.

## How this project is developed

The maintainer develops Dots Brain with AI coding assistants. Independent review
means a separate AI reviewer unless a report names a human reviewer. Tests and
review reports document what was exercised; the maintainer owns release decisions.
Reviewers in one orchestration share the workspace and permissions. Their separate
review tasks do not establish process or operating-system isolation.

## License

MIT. Dots Brain is independent of AI providers. Third-party dependencies and model
artifacts retain their own licenses. See [third-party notices](THIRD_PARTY_NOTICES.md).
