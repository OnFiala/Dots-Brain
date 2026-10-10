# Linux deployment template

The files in this directory are examples for a single Linux host. They are not a
record of any existing installation. Replace `memory.example.com`, the private
addresses, executable path, service user, and data directory before use.

## What the template provides

- `systemd/dots-brain.service` runs a loopback HTTP MCP server.
- `systemd/dots-brain-ingress.service` starts a small dedicated nginx gateway.
- `systemd/dots-brain-onboarding.service` opens the gateway pairing gate for ten
  minutes when explicitly started.
- `nginx/dots-brain.conf` is an example gateway configuration. It uses
  `memory.example.com` and TEST-NET-1 placeholders `192.0.2.1` and `192.0.2.2`.
  Replace both addresses before use; they are documentation-only values.

The nginx configuration permits MCP bodies up to 1 MiB and limits OAuth endpoint
bodies to 16 KiB. It accepts only the expected methods, removes forwarded-header
input, sets the public-gateway marker, and sends unknown paths to 404.

## Prepare an instance

1. Create a dedicated unprivileged service account and a private data directory.
2. Check out a reviewed version and create its environment with `uv sync --locked
   --extra semantic` if semantic search is wanted.
3. Initialize the store, prepare the model if selected, and stop any test service.
4. Copy and edit the unit and nginx files to the host's system locations.
5. Validate the final nginx configuration and systemd units with the host tools.
6. Start the memory service, then the gateway. Verify a local MCP read before
   enabling OAuth.

Do not run both `dots-brain up` and the systemd memory service for one data
directory. The systemd unit owns the long-running HTTP process. Keep the service
loopback-only; an existing TLS terminator or tunnel is a separate operator choice.

## OAuth onboarding

Opening the CLI onboarding window alone is insufficient when the gateway example
is used. Start the short gateway unit for the same time window, then approve the
exact client request with its callback hostname and intended projects. Close the
window after the flow and check the resulting grant. See [OAuth](../docs/oauth.md).

This template needs host-specific review before use. It does not verify TLS,
public reachability, client UI activation, reboot recovery, or backup recovery.
