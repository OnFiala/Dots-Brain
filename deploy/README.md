# Linux deployment examples

These templates are examples for one Linux host, not an observed installation.
Replace all domain, address, user, interpreter and data-directory placeholders.
Deployment, TLS routing and restarts need their own approved target and rollback.
Template validation and full operational hardening are next-batch work.

`systemd/dots-brain.service` runs one HTTP process with private port 8765 and
OAuth-only port 8787. The ingress unit and nginx example route public traffic to
8787. Static credentials must never pass on that listener, regardless of Host.
The example gateway allows larger MCP bodies than the backend, which caps MCP
at 128 KiB and OAuth at 16 KiB. The backend remains the final limit.

## Minimal sequence for an authorized deployment

1. Record the exact artifact, service owner, canonical directory and recovery plan.
2. Initialize a new store or validate an authorized copy through [upgrade](../docs/upgrading.md).
3. With writers stopped, configure the intended HTTPS issuer using [OAuth](../docs/oauth.md).
4. Edit and validate the unit/gateway with the host's actual systemd/nginx tools.
5. Start one service through that supervisor. Use `doctor --mcp-url` with an
   existing private credential; use `connect --mcp-url` for a local adapter.
6. Verify private/public authentication and the real client approval/callback.
   A 401 without access is not proof of successful pairing or memory use.

Never run managed `up` beside the supervised HTTP service for the same store.
`down` does not stop an external service. Restart or stop it through its owner.
See [installation](../docs/installation.md) for exact private endpoint commands.

The optional `dots-brain-onboarding.service` controls an nginx gate only. This v2
application has no CLI onboarding-window command. When that gateway gate is used,
its operator must open it for the initiated pairing flow and then close it;
application approval still targets the exact request on the memory host.

This checkpoint's Linux coexistence case is awaiting supervisor verification.
An external process test does not prove a real systemd unit, boot startup, TLS,
public reachability or restoration of real data. Those are separate acceptance
steps. A binary rollback cannot undo revocation, FTS changes or data mutations.
