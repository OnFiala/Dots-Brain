# Dots Brain

**One memory for your AI tools, hosted on your own machine.**

Dots Brain is an open-source memory service for an OpenAI Dot and other MCP-compatible assistants. It stores useful context with its sources, retrieves relevant memories, and lets connected assistants continue each other's work. The target deployment is your Dot's VM, with local storage and local embeddings and no paid model API.

**Status: OAuth alpha (`0.3.0-alpha.2`).** An agent can start the shared service, configure supported local clients, authorize OAuth clients on the VM, and remove the integration while preserving memories. Public ingress, automatic web-account setup, conversation capture, and native ChatGPT views remain planned. Deployment on a Dot VM is not yet verified. See the [capability matrix](docs/capabilities.md).

Tell your agent: "Install Dots Brain on my memory host and connect my supported
AI tools. Follow the repository's setup skill and verify the connections."

## What works today

- One personal memory store, shared through MCP.
- Searchable memories with source references, revisions, and explicit deletion.
- Local text and semantic search without a paid embedding API.
- Local setup, diagnostics, and client verification with machine-readable status.
- Repeatable background startup and client adapters for Claude Code, Cursor, Codex, and MCP JSON.
- Six memory tools through stdio or authenticated loopback HTTP.
- A stdio bridge that connects to the same HTTP service without another database.
- Client disconnection and repeatable service removal that preserves memories.
- VM-local OAuth with PKCE, scoped owner approval, refresh, and revocation.

Provider capture adapters, autonomous web setup, native ChatGPT views, and
event-driven automations are on the [roadmap](docs/roadmap.md).

An MCP connection gives an assistant access to memory. It does **not** automatically grant access to that assistant's conversations or account history.

## Try the alpha

From a writable checkout with Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --frozen
uv run dots-brain setup
uv run dots-brain doctor
uv run dots-brain serve
```

`serve` starts a stdio MCP server for a client on the memory host. The default data
directory is `$XDG_DATA_HOME/dots-brain`, or `~/.local/share/dots-brain`. To choose a
different private directory, pass `--data-dir /absolute/path` before the subcommand.

For a shared HTTP instance, scoped credentials, local embeddings, or agent-driven
bootstrap, follow [installation and operation](docs/installation.md).

## Give the setup to your agent

The repository includes an onboarding plugin manifest and a [setup skill](skills/setup/SKILL.md).
The agent should identify the real memory host, run the packaged bootstrap, and
verify the available capabilities. This is an installer skill package; it does not
contain a universal MCP endpoint or register a public ChatGPT plugin automatically.

Ask: "Set up Dots Brain from this checkout on my memory host. Use the setup skill
and report which capabilities you actually verified."

On a Linux memory host, the agent can install, start, and connect a local client:

```sh
python scripts/bootstrap.py --data-dir /absolute/private/memory --connect claude-code
```

Use `cursor` or `codex` for another supported client on that machine. The agent
chooses the actual private directory; the user does not need to edit JSON or copy
a token. See the [autonomous setup contract](docs/autonomy.md) for remote devices,
resuming installation, and the exact remaining platform dependencies.

For a client that supports remote MCP OAuth, see [OAuth on your VM](docs/oauth.md).
The authentication service runs beside the memory; an actual reachable HTTPS
route is still required. Configuring an issuer does not provision a tunnel.

## Leaving or updating

Ask your agent: "Uninstall Dots Brain, disconnect its managed clients, and keep my
memories." The `uninstall` command disables the instance and preserves personal
data; program files are removed separately according to the installation method.
See [uninstall and reinstall](docs/uninstall.md), [upgrading](docs/upgrading.md),
and [troubleshooting](docs/troubleshooting.md).

The [documentation index](docs/README.md) covers the implemented alpha's lifecycle
and [everyday memory use](docs/using-memory.md), with remaining limits explicit.

## Development

```sh
uv sync --frozen --all-extras
uv run ruff check src tests scripts
uv run pytest -q
uv run python scripts/validate_project.py
uv build
```

Tests use synthetic data. The real embedding-model test is opt-in and does not
download models during CI. See [verification evidence](docs/verification.md).

## Deployment contract

The memory database, embedding inference, authentication, and installation state belong on the user's host. A public HTTPS ingress or a compatible tunnel is needed for remote clients. Hosting and resource limits must be verified; an existing subscription is not a promise of unlimited compute or free external services.

ChatGPT Sites is a separate hosting option under investigation, not the default deployment. No data is silently moved there.

Read the [architecture](docs/architecture.md), [capabilities](docs/capabilities.md), and [roadmap](docs/roadmap.md).

## Contributing and license

Code, documentation, CLI messages, issues, and commit messages are written in English. See [CONTRIBUTING.md](CONTRIBUTING.md).

Licensed under [MIT](LICENSE): personal and commercial use, modification, and redistribution are permitted under its terms. Third-party dependencies and model artifacts retain their own licenses. Dots Brain is an independent project and is not affiliated with OpenAI or other AI providers.
