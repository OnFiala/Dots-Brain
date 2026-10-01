# Dots Brain

**One memory for your AI tools, hosted on your own machine.**

Dots Brain is an open-source memory service for an OpenAI Dot and other MCP-compatible assistants. It stores useful context with its sources, retrieves relevant memories, and lets connected assistants continue each other's work. The target deployment is your Dot's VM, with local storage and local embeddings and no paid model API.

**Status: early development.** The first alpha is being built. This is not yet a one-click product, a complete conversation recorder, or a verified deployment on a Dot VM. See the [capability matrix](docs/capabilities.md) before relying on an integration.

## What we are building

- One personal memory store, shared through MCP.
- Searchable memories with source references, revisions, and explicit deletion.
- Local text and semantic search without a paid embedding API.
- Agent-driven installation with honest, machine-readable connection status.
- Small provider adapters for supported hooks, APIs, and imports.
- Native ChatGPT views and event-driven automations where supported.

An MCP connection gives an assistant access to memory. It does **not** automatically grant access to that assistant's conversations or account history.

## Deployment contract

The memory database, embedding inference, authentication, and installation state belong on the user's host. A public HTTPS ingress or a compatible tunnel is needed for remote clients. Hosting and resource limits must be verified; an existing subscription is not a promise of unlimited compute or free external services.

ChatGPT Sites is a separate hosting option under investigation, not the default deployment. No data is silently moved there.

Read the [architecture](docs/architecture.md), [capabilities](docs/capabilities.md), and [roadmap](docs/roadmap.md).

## Contributing and license

Code, documentation, CLI messages, issues, and commit messages are written in English. See [CONTRIBUTING.md](CONTRIBUTING.md).

Licensed under [MIT](LICENSE): personal and commercial use, modification, and redistribution are permitted under its terms. Third-party dependencies and model artifacts retain their own licenses. Dots Brain is an independent project and is not affiliated with OpenAI or other AI providers.
