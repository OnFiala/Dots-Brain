---
name: setup
description: Install, inspect, or connect Dots Brain on an authorized memory host. Use when the user requests shared AI memory or asks to connect an existing Brain instance.
---

# Set up Dots Brain

Read the package's `README.md`, `docs/capabilities.md`, and `docs/installation.md`.
Also read `docs/autonomy.md`. This alpha automates local service startup, private
credentials, supported client configuration, and real bridge read/write checks.
Remote OAuth, automatic capture, a public tunnel installer, and an isolated
credential broker are not implemented. Keep those states explicit.

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
python scripts/bootstrap.py --data-dir <absolute-personal-data-directory> --semantic --connect <provider>
```

Use `claude-code`, `cursor`, or `codex` when that application is on the memory host.
Omit `--connect` if the request is only to install the service. This installs
locked dependencies and the pinned CPU model, reuses the database, starts the
background service, and configures and verifies the generated client bridge.
The model download is about 241 MiB. Run `preflight` and `doctor`; distinguish
working local startup from verified VM persistence or public ingress.

Startup and connection commands are resumable. Repeat the same command after a
recoverable interruption; do not generate new credentials or new stores yourself.
`up` starts or reuses one loopback HTTP service. Configured local bridges restart
it on reconnect if it stopped. This is not a persistent supervisor or VM boot
service. Public ingress and web-client OAuth remain a separate milestone.

## Connect another client

Discover the client's actual machine and supported transport. For a client on the
memory host, run `dots-brain --data-dir <existing-directory> connect <provider>`.
Use `--config <actual-path>` for a nonstandard configuration location, or the
`mcp-json` adapter for a compatible client. The command verifies its generated
bridge, preserves other settings, and writes no tokens into application config.
It tests writes with an explicitly synthetic probe and removes that probe.

The installer writes a scoped credential directly to a private file and returns
metadata only. Never read that file into model context,
print it, embed its token in a command, or put it in a repository. Filesystem access
still permits the local OS user to read it: this is not strong secret isolation.
Do not assume a credential on the VM has been safely delivered to a laptop.
For a different device, use its authorized execution path and an existing securely
provisioned connection file with `connect <provider> --credential-file <path>`.
That flow verifies reads and never initializes a second database. If access to
the actual device is missing, report `awaiting_device` with that specific reason.

Where installed, also use the application's own health check; for Claude Code,
`claude mcp get dots-brain` reports the connection. Do not claim activation inside
another application's conversation from the SDK bridge test alone. Do not disable
the application's trust prompts or authentication controls. Reuse already granted
authority rather than asking the user for it again.

For web clients, inspect `preflight` before attempting a tunnel. A managed network
with no configured TCP destinations is not a working tunnel transport. Do not
change platform policy, invent an endpoint, or move the store to Sites or another
host. Continue all independent local setup and return the exact missing platform
capability once, without asking the user to copy tokens or edit JSON.

## Report the result

Return the instance host, data directory, version, actual verified transport, and
separate read/write/capture/history-import states. Include only verified addresses.
Use `awaiting_auth`, `awaiting_device`, or `blocked` only with a concrete reason.
Do not ask the user to perform commands you can already run within their authority.
Do not export, erase, or move personal memory merely to complete a connectivity test.
