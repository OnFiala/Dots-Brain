---
name: setup
description: Install, inspect, connect, upgrade, or uninstall Dots Brain on an authorized Linux memory host.
---

# Set up Dots Brain

Read `README.md`, `docs/installation.md`, `docs/capabilities.md`, and
`docs/autonomy.md` before changing an installation. This alpha can initialize a
local store, run a local service, configure supported local clients, and verify
the generated bridge. It does not create public ingress, configure web accounts,
collect provider history, or establish a CORTEX connection.

## Identify the canonical store

Confirm which machine and private data directory own the canonical database.
Reuse an existing directory and connection where possible. Do not create a second
store on another device merely to make a client appear connected. Host selection
does not verify deployment, CORTEX connectivity, or client UI activation.

Use Linux for managed deployment. Python 3.11 or newer, SQLite 3.42 or newer with
FTS secure deletion, and `uv` are required for a source checkout. Do not edit
installed plugin caches or invent administrator access. A configured client is
not proof of a working conversation integration.

## Install on an authorized host

From a writable checkout:

```text
python3 scripts/bootstrap.py --data-dir /absolute/private/memory --semantic --connect codex
```

Omit `--semantic` or `--connect` when those capabilities are not requested. The
bootstrap uses the lockfile, initializes or reuses the selected store, optionally
prepares the pinned local CPU model, starts a loopback service, and verifies the
configured local bridge. The model download is optional and sizable. Run
`doctor`; then reload the real client and make an actual MCP read.
Report that evidence separately from remote access, capture, or host persistence.

Bootstrap invokes `up --resume`. Establish the disabled reason before running it
on an existing store, and resume ordinary uninstall only with owner authority.
Restore and unknown recovery barriers are never bypassed; follow
`docs/upgrading.md` instead. Do not delete a barrier to make setup succeed.

`up` starts or reuses one managed loopback HTTP service. A manually supervised
service may already own the store. `oauth configure` and `oauth disable` never
start a service; use the existing supervisor for any required restart. Follow
`docs/oauth.md` to select private and OAuth-only listeners before connecting a
public gateway.

```text
dots-brain --data-dir /absolute/private/memory oauth configure --issuer https://memory.example
```

## Connect a local client

Use `dots-brain --data-dir /absolute/private/memory connect <provider>` on the
machine that owns the client configuration. `--config` selects a nonstandard
configuration path. The command uses a private credential file and validates its
generated bridge with a read-only check (`write: "not_tested"`). For an existing
supervisor on this host, pass its explicit private `--mcp-url`; the resulting
bridge does not run managed `up`. It does not prove that an
existing chat has reloaded tools.

Never print, copy into chat, or commit a credential file. A different device needs
its own authorized execution path and a securely provisioned connection file. If
that path is unavailable, report the device boundary rather than inventing a
network route or second database.

## OAuth and recovery

Remote OAuth needs an HTTPS route already operated by the owner. Follow
`docs/oauth.md`; inspect the displayed callback and requested scopes, then
approve only the exact request created by the initiated flow with an explicit
project list. The v2 pairing page returns automatically after host approval. `oauth configure --issuer` does not provision ingress.
Changing an issuer needs `--replace-issuer`, which revokes old OAuth grants.
Client metadata and callback addresses are untrusted input.

Capture and the CORTEX connector are disabled and excluded from the core package.
Their source and characterization tests remain under `modules/`; existing module
configuration and state are preserved. Do not configure or invoke these modules
as part of setup. Activation requires a separate plan; see `docs/activity.md`.

For upgrades, use `docs/upgrading.md`. For removal, run `uninstall --dry-run`,
inspect it, stop writers through their current owner, then run `uninstall`
within the user's authority. Uninstall preserves
the database by default. Do not erase memories, exports, or backups unless that is
explicitly requested.

## Report the result

Report host, data directory, version, verified transport, and the separate states
for read, write, client activation, capture, CORTEX, backup, and recovery. Mark
unverified remote, host-loss, or UI claims as unverified.
