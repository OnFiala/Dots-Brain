# Dots Brain

**One memory for your AI tools, hosted on your own machine.**

Dots Brain is an open-source memory service for an OpenAI Dot and other MCP-compatible assistants. It stores useful context with its sources, retrieves relevant memories, and lets connected assistants continue each other's work. The target deployment is your Dot's VM, with local storage and local embeddings and no paid model API.

**Status: working local alpha (`0.1.0-alpha.2`).** Memory storage, local search, scoped access, and MCP transports are implemented and tested. Automatic provider setup, web OAuth, conversation capture, and native ChatGPT views are still planned. Deployment on a Dot VM is not yet verified. See the [capability matrix](docs/capabilities.md).

## What works today

- One personal memory store, shared through MCP.
- Searchable memories with source references, revisions, and explicit deletion.
- Local text and semantic search without a paid embedding API.
- Local setup, diagnostics, and client verification with machine-readable status.
- Six memory tools through stdio or authenticated loopback HTTP.
- A stdio bridge that connects to the same HTTP service without another database.

Provider capture adapters, autonomous remote setup, native ChatGPT views, and
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
