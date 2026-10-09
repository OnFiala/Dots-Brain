# Appliance ingress and bot onboarding

This is the deployment contract for the candidate gateway. A configured route is
not proof that either bot connected. Record runtime and client acceptance separately
in [appliance deployment](appliance-deployment.md).

## One public application, separate owner access

The chosen origin is `https://dots-brain.ofops.co`; publish it only after the local
gateway checks pass. Memory remains on `openclaw-appliance`. Owner commands use
the existing Tailscale SSH route. Each bot receives a separate OAuth grant with
`memory:read memory:write` for project `shared`, without deletion or CORTEX scopes.

The existing Cloudflare tunnel `agent-workspace-mcp` can route this additional
hostname to `http://172.17.0.1:8788`. Cloudflare terminates public TLS and can see
application traffic; its tunnel encrypts transport to the appliance connector.
The last HTTP hop stays on the local Docker bridge. Do not add Cloudflare Access
login in front of the OAuth endpoints: the bot's backend must reach discovery and
token exchange. Dots Brain OAuth is the memory authorization boundary.

The connector shares `agent-workspace-v1`'s network namespace. On 2026-10-09 its
bridge IP was `172.17.0.2`, gateway `172.17.0.1`. Other processes in that namespace
can also reach the gateway, still subject to OAuth and request limits. The source
ACL is not isolation between those processes. Verify these addresses before
installation and after container replacement. Changed addresses fail closed;
do not broaden the ACL to a LAN, tailnet, or all Docker containers to repair it.

## Dedicated gateway

- `deploy/nginx/dots-brain.conf` is a standalone nginx configuration for this host.
- Install it at `/etc/dots-brain/ingress.conf`, root-owned and mode `0644`.
- Install `deploy/systemd/dots-brain-ingress.service` and
  `deploy/systemd/dots-brain-onboarding.service` under `/etc/systemd/system/`.
- The gateway runs under a dynamic user and binds only `172.17.0.1:8788`.
  It forwards allowed paths to `127.0.0.1:8787` using the fixed public Host.
  The existing host nginx service and other tunnel routes are separate.
- Total load is bounded to 20 requests/second with a burst of 40 and 16 active
  requests. DCR and authorization have lower limits. Bodies are capped at 128 KiB
  for MCP and 16 KiB for OAuth. Limits are global, not based on untrusted headers.
- Access logging is off and nginx error output is discarded to prevent request
  URLs, authorization codes or pairing IDs entering logs. Use unit state and
  sanitized HTTP probes for gateway diagnosis. Do not temporarily enable payload
  logging during a real client connection.

Validate with `nginx -t -c /etc/dots-brain/ingress.conf` and systemd unit validation.
Start through `systemctl enable --now dots-brain-ingress.service`. Configure OAuth
with the documented `--no-start` supervisor procedure. A consistent private
database backup and the previous immutable release must exist before replacement.

## Short owner-controlled onboarding window

Registration, authorization and pairing return `503` by default. Discovery,
authenticated MCP, token renewal and revocation remain available. Open a ten-minute
window only while connecting an identified bot:

```sh
sudo systemctl start dots-brain-onboarding.service
```

The unit creates a non-secret marker in its runtime directory, sleeps for ten
minutes, then removes the directory automatically. It is not enabled on boot.
Stopping or restarting the gateway also stops this window. Close early with:

```sh
sudo systemctl stop dots-brain-onboarding.service
```

Wait for the actual client pairing request. Approve only its observed request ID
with `--project shared --scope memory:read --scope memory:write`. Keep the window
open until the client completes the callback. If it expires, open another window
and restart that client's connection flow. New consent after grant expiry also
needs an owner window. Never approve another request merely because its name
looks familiar. Do not copy tokens or client secrets into chat.

## Acceptance and rollback

Before publishing, probe from the connector namespace: discovery issuer/resource,
anonymous MCP `401`, wrong Host/default paths/pairing suffix denied, oversized body
`413`, bounded bursts `429`, closed onboarding `503`, open/close and timed cleanup.
From outside that namespace verify the source ACL denies access. Exercise the full
official SDK OAuth/MCP flow with synthetic scoped data; revoke the synthetic grant.
Then verify public DNS, TLS and these same route boundaries over HTTPS.

Connect each actual bot separately. Each must report a successful `memory_status`,
write one synthetic shared fact and read the other's exact ID/revision. This is
client acceptance, independent of SDK tests. Owner cleanup does not require giving
bots deletion scope. Only then import the bots' available context directly into
memory. This does not enable complete conversation or action capture.

Rollback first stops onboarding and the dedicated gateway, then removes only this
new hostname route. For server rollback stop Dots Brain, disable OAuth with
`--no-start` if needed, select the previous immutable release and restart its unit.
Preserve the memory database and backup; do not restore an older database over new
memories or deletion records. Existing tunnel hostnames and host nginx stay intact.
