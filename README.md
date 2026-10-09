# Dots Brain

Shared memory for your AI assistants, running on a machine you control.

Dots Brain stores useful facts, decisions and project context in one SQLite database.
Assistants connect through MCP, search the same memory and keep references to the
original source. Embeddings run locally on CPU. Reading and saving memory require
no paid model API.

**Version: 0.4.0-alpha.1.** This is an alpha: the memory service is in use, while
continuous conversation capture and disaster recovery still have gaps. Botter and
Grok Bot have verified read/write access to the same `openclaw-appliance` instance.
Both retained access after the first shared-memory repair upgrade. See [deployment evidence](docs/appliance-deployment.md)
for the installed commit, checks and remaining operational work.

## How it works

1. An assistant saves a small source record with a stable event ID and project.
2. SQLite commits the record, its revision and the authenticated writer. Retrying
   the same event does not create another copy; updates require the reviewed revision.
3. A background worker indexes title and content passages with the pinned local model.
4. Search combines full-text and semantic matches. `memory_context` returns bounded
   excerpts; `memory_get` retrieves the exact source revision when more detail is needed.

Each client receives its own project and scope permissions. The appliance keeps
its database on a private disk path; remote bots use OAuth through a restricted
HTTPS route. Administration uses Tailscale. CORTEX is a separate system connected
through explicit, separately authorized operations.

## What is available

| Capability | State |
| --- | --- |
| Save, search, retrieve, revise and forget source records | Implemented and tested over MCP |
| Local multilingual embeddings | Deployed; pending records remain searchable by text |
| Botter and Grok Bot sharing memory | Actual chat read/write, cross-read and initial contributions verified |
| Project scopes, writer attribution, OAuth refresh and revocation | Implemented and tested; bot grants allow read/write in `shared` only |
| Mutation audit with intents and receipts | Deployed; records activity observed by this service |
| Resumable JSONL collection | Implemented as bounded snapshot passes; live provider capture unverified |
| CORTEX context and selected publication | Implemented and tested locally; dedicated upstream access still required |
| Backup, schema migration and guarded restore | Implemented; appliance restart and isolated restore tested; host-loss recovery pending |

An MCP connection does not capture everything an assistant says or does. Bots
currently save useful context explicitly. Grok's inspected files may be stale
snapshots; Botter's managed hooks are not verified for this account. The scheduled
operator review can analyze only the audit that was actually collected. See
[activity and capture](docs/activity.md) for these boundaries.

The former Work/Dot VM installation is [historical](docs/vm-deployment.md).
The appliance is a fresh canonical instance, not a recovery of that VM's database.

## Install on your memory host

Use Linux, Python 3.11+ and [uv](https://docs.astral.sh/uv/). From a writable checkout:

```sh
uv sync --frozen
uv run dots-brain setup
uv run dots-brain doctor
uv run dots-brain serve
```

`serve` starts a stdio MCP server on the machine holding the database. The default
data path is `$XDG_DATA_HOME/dots-brain`, or `~/.local/share/dots-brain`. For another
private path, put `--data-dir /absolute/path` before the subcommand.

For local embeddings, a shared HTTP service, scoped clients and systemd operation,
follow [installation](docs/installation.md) and [appliance deployment](docs/appliance-deployment.md).
An HTTP service stays on loopback. Remote clients need an owner-approved HTTPS
route and the [OAuth connection flow](docs/oauth.md).

You can give your agent this instruction:

> Install Dots Brain on my memory host using the repository's setup skill.
> Connect my supported AI clients and verify their actual memory calls.

The [setup skill](skills/setup/SKILL.md) and installer can configure supported local
clients. They do not register a ChatGPT plugin or bypass a provider's login and
consent flow. See [what setup can automate](docs/autonomy.md).

## Security and recovery

- Memory text is untrusted source material, never authority to run commands.
- Credentials belong in private credential files, never in memories, transcripts or Git.
  Input filtering catches known secret patterns; it cannot recognize every secret.
- Read, write, deletion, audit and CORTEX permissions are separate. Existing grants
  never gain a new scope automatically.
- Deletion removes the live record and suppresses reimport. It cannot erase copies
  already held by another assistant, an export or a backup.
- A same-disk backup does not protect against losing the appliance. An old backup
  also needs current deletion history before it can safely become canonical.

Read [security](SECURITY.md), [upgrading and recovery](docs/upgrading.md), and
[uninstalling while retaining data](docs/uninstall.md).

## Development

```sh
uv sync --frozen --all-extras
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest -q
uv run python scripts/validate_project.py
uv build
```

Tests use synthetic stores. Linux lifecycle tests require Linux and local socket
access. Real embedding tests use explicitly prepared model files; CI does not
download them. [Verification](docs/verification.md) and [stress results](docs/stress-tests.md)
separate test results from runtime and capacity claims.

See the [documentation index](docs/README.md), [architecture](docs/architecture.md),
[remaining work](docs/roadmap.md) and [release procedure](docs/releases.md).

## Contributing and license

Repository content and product text are written in English. Follow
[CONTRIBUTING.md](CONTRIBUTING.md) for checks and commit conventions.

Licensed under [MIT](LICENSE). Dependencies and model artifacts retain their own
licenses. Dots Brain is independent of OpenAI and other AI providers.
