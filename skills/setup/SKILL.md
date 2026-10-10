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

Use Linux for managed deployment. Python 3.11–3.13, SQLite 3.42 or newer with
FTS secure deletion, and `uv` are required for a source checkout. Do not edit
installed plugin caches or invent administrator access. A configured client is
not proof of a working conversation integration.

## Install on an authorized host

From a writable checkout:

```text
python scripts/bootstrap.py --data-dir /absolute/private/memory --semantic --connect codex
```

Omit `--semantic` or `--connect` when those capabilities are not requested. The
bootstrap uses the lockfile, initializes or reuses the selected store, optionally
prepares the pinned local CPU model, starts a loopback service, and verifies the
configured local bridge. The model download is optional and sizable. Run
`preflight` and `doctor`; then reload the real client and make an actual MCP read.
Report that evidence separately from remote access, capture, or host persistence.

`up` starts or reuses one managed loopback HTTP service. A manually supervised
service may already own the store. `oauth configure` and `oauth disable` never
start a service; use `--no-start` in supervised automation for an explicit stable
command contract.

```text
dots-brain --data-dir /absolute/private/memory oauth configure --issuer https://memory.example --no-start
```

## Connect a local client

Use `dots-brain --data-dir /absolute/private/memory connect <provider>` on the
machine that owns the client configuration. `--config` selects a nonstandard
configuration path. The command uses a private credential file and validates its
generated bridge with synthetic memory operations. It does not prove that an
existing chat has reloaded tools.

Never print, copy into chat, or commit a credential file. A different device needs
its own authorized execution path and a securely provisioned connection file. If
that path is unavailable, report the device boundary rather than inventing a
network route or second database.

## OAuth, capture, and recovery

Remote OAuth needs an HTTPS route already operated by the owner. Follow
`docs/oauth.md`; onboarding is closed by default, so open a short window and
approve only the exact request created by the initiated flow. Approval requires
the exact callback hostname through `--redirect-host`, requested scopes, and an
explicit project list. `oauth configure --issuer` does not provision ingress.
Changing an issuer needs `--replace-issuer`, which revokes old OAuth grants.
Client metadata and callback addresses are untrusted input.

Capture accepts an opt-in bounded JSONL snapshot. It excludes analysis/reasoning
payloads and is not provider-wide or live conversation capture. CORTEX is optional;
use it only with a separately authorized endpoint, token file, project map, and
live verification. Inspect uncertain CORTEX operations before retrying; only an
idempotent note can receive an owner-authorized retry.

For upgrades, use `docs/upgrading.md`. For removal, run `uninstall --dry-run`,
inspect it, then run `uninstall` within the user's authority. Uninstall preserves
the database by default. Do not erase memories, exports, or backups unless that is
explicitly requested.

## Report the result

Report host, data directory, version, verified transport, and the separate states
for read, write, client activation, capture, CORTEX, backup, and recovery. Mark
unverified remote, host-loss, or UI claims as unverified.
