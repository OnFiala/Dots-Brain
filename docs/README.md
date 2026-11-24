# Documentation

These source documents describe the Linux core in the unreleased 1.0.2 default
branch: explicitly saved notes, source references, revisions, full-text search and optional local CPU embeddings.
Capture and CORTEX remain disabled; individual assistant behavior requires its
own verification.

The latest published release is **[1.0.1](https://github.com/OnFiala/Dots-Brain/releases/tag/v1.0.1)**.
Use its [versioned documentation index](https://github.com/OnFiala/Dots-Brain/blob/v1.0.1/docs/README.md)
for that installation. The installation page below targets 1.0.1; the remaining
local links describe current source and do not establish a 1.0.2 publication.

## Start here

- [Installation](installation.md): pinned release assets, hash-locked source/wheel setup and supported platforms.
- [Using memory](using-memory.md): source identity, limits, and scopes.
- [CLI reference](reference/cli.md): commands, JSON states, and operator boundaries.
- [MCP tools](reference/mcp-tools.md): tool names, scopes, limits, and typed errors.
- [Troubleshooting](troubleshooting.md): safe responses to common states and errors.

## Operation and recovery

- [Activity and capture](activity.md): audit data, snapshot capture, and review limits.
- [OAuth](oauth.md): owner-approved remote access, fixed 30-day grants and re-pairing.
- [Upgrading and recovery](upgrading.md): offline migration, backup, rollback, and staged restore.
- [Uninstall](uninstall.md): disable an instance while retaining data.
- [Verification](verification.md): source checks versus runtime acceptance.
- [Security](../SECURITY.md): trust boundaries and reporting.
- [Linux deployment](../deploy/README.md): generic systemd and reverse-proxy template.

## Design and project status

- [Architecture](architecture.md): local components and data boundaries.
- [Capabilities](capabilities.md): implemented surfaces and acceptance boundaries.
- [Autonomy](autonomy.md): authorized autonomous setup and verification boundaries.
- [Roadmap](roadmap.md): planned work.
- [Stress tests](stress-tests.md): bounded synthetic measurements and their limits.
- [Releases](releases.md): tag and draft-release procedure.
