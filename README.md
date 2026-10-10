# Dots Brain

Dots Brain is a self-hosted MCP memory service for explicitly saved notes. It
stores source metadata and revisions in one SQLite database, supports full-text
search, and can add local CPU embeddings. It does not automatically collect
assistant conversations.

**Version: 0.4.0-alpha.2 (unreleased).** This candidate has local MCP storage,
scoped client credentials, backup and staged restore commands. It is not a
published release or a deployment claim. Live capture, a CORTEX connection,
off-host recovery, and host reboot recovery need separate configuration and
acceptance evidence.

## Quick start on Linux

Use a writable checkout, Python 3.11–3.13, and [uv](https://docs.astral.sh/uv/).
There is no supported `pip install` path in this alpha.

```sh
git clone https://github.com/OnFiala/Dots-Brain.git
cd Dots-Brain
uv sync --locked
uv run dots-brain --data-dir /absolute/private/memory setup
uv run dots-brain --data-dir /absolute/private/memory doctor
```

For repeatable work, check out a reviewed commit or release tag before syncing.
Python must provide SQLite 3.42 or newer. The data, backup, and export directories
must be on a local POSIX filesystem that supports hard links; network shares and
filesystems without that capability fail with `capability_unavailable`.

For several clients on the same Linux host, use `up` to run an authenticated
loopback HTTP service, then configure a supported local adapter. See
[installation](docs/installation.md).

```sh
uv run dots-brain --data-dir /absolute/private/memory up
uv run dots-brain --data-dir /absolute/private/memory connect codex --project work
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
- An MCP connection gives access only to explicitly saved memory. Capture is an
  opt-in bounded JSONL import, not continuous provider capture.
- Remote OAuth needs an existing HTTPS route and an explicit owner approval flow.
  Dots Brain does not provision public ingress or configure web accounts.
- CORTEX is a separate service for project context, decisions and outcomes.
  Its optional connector needs its own credential, project mapping, and
  live verification. It is not enabled by installation.
- Backups on the same disk do not recover a lost host. Restore requires a current
  store for deletion reconciliation; full host-loss recovery is not yet supported.

Read [security](SECURITY.md), [upgrading and recovery](docs/upgrading.md), and the
[documentation index](docs/README.md) before operating a long-lived store.

## Development

```sh
uv sync --locked --all-extras
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest -q
uv run python scripts/validate_project.py
uv build
```

Tests use disposable stores. CI validates Python 3.11, 3.12, and 3.13. See
[verification](docs/verification.md) for the distinction between source checks
and deployment acceptance.

## How this project is developed

The maintainer develops Dots Brain with AI coding assistants. Independent review
means a separate AI reviewer unless a report names a human reviewer. Tests and
review reports document what was exercised; the maintainer owns release decisions.
Reviewers in one orchestration share the workspace and permissions. Their separate
review tasks do not establish process or operating-system isolation.

## License

MIT. Dots Brain is independent of AI providers. Third-party dependencies and model
artifacts retain their own licenses. See [third-party notices](THIRD_PARTY_NOTICES.md).
