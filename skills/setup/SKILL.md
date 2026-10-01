---
name: setup
description: Install, inspect, or connect Dots Brain on an authorized memory host. Use when the user requests shared AI memory or asks to connect an existing Brain instance.
---

# Set up Dots Brain

Read the package's `README.md`, `docs/capabilities.md`, and `docs/installation.md`.
This alpha provides local memory, MCP tools, and a connection bridge. It does not
yet implement remote OAuth, automatic capture, a public tunnel installer, or an
isolated credential broker. Never describe those capabilities as complete.

## Identify the host and instance

Determine which machine owns the canonical memory. A development workspace is
not automatically the user's Dot VM, and the current machine is not automatically
the user's laptop. Reuse an existing data directory and connection. Never create
a second independent memory on another device to make its client appear connected.

Use a writable checkout of the requested release. Do not edit installed plugin
caches. Python 3.11+ and `uv` are required for the source bootstrap. Use the user's
existing installation where available. Follow the host's supported package
installation process for missing prerequisites; do not invent administrator access.

## Install on the authorized memory host

Run the packaged installer from the checkout:

```text
python scripts/bootstrap.py --data-dir <absolute-personal-data-directory> --semantic
```

This installs locked dependencies and the pinned multilingual CPU model, and
initializes or reuses the database. It does not start a durable background service.
The model download is about 241 MiB. Use the JSON result and run `doctor`; distinguish
local setup from verified VM persistence, ingress, and client connectivity.

Use stdio only for a client running on the memory host. For a shared instance,
run the authenticated HTTP service using a process supervisor actually supported
by the host. The alpha binds to loopback. Do not expose it publicly or fabricate
a URL. Public ingress and web-client OAuth remain a separate milestone.

## Connect another client

Discover the client's actual version, machine, and supported transport. Reuse a
working connection. The alpha's stdio bridge connects to the existing HTTP instance;
it does not store a second copy of the memory.

The owner-side `client create` command writes a scoped credential directly to a
new private file and returns metadata only. Never read that file into model context,
print it, embed its token in a command, or put it in a repository. Filesystem access
still permits the local OS user to read it: this is not strong secret isolation.
Use the client application's supported configuration flow, preserving unrelated
settings. Do not assume a credential on the VM has been safely delivered to a laptop.

Run `dots-brain verify --credential-file <absolute-private-file>` on the client
machine to verify a real MCP read. Its result explicitly leaves write and capture
unverified. Do not report all capabilities ready from this read-only check. For an
unsupported web, device, or auth flow, report the specific missing capability and
retain the existing working setup.

## Report the result

Return the instance host, data directory, version, actual verified transport, and
separate read/write/capture/history-import states. Include only verified addresses.
Use `awaiting_auth`, `awaiting_device`, or `blocked` only with a concrete reason.
Do not ask the user to perform commands you can already run within their authority.
Do not export, erase, or move personal memory merely to complete a connectivity test.
