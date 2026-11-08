# Documentation

Dots Brain 1.0.0 documents the Linux core: explicitly saved notes, source
references, revisions, full-text search and optional local CPU embeddings.
Capture and CORTEX remain disabled; individual assistant behavior requires its
own verification.

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
