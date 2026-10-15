# Linux deployment examples

These templates are examples for one Linux host, not an observed installation.
Replace all domain, address, user, interpreter and data-directory placeholders.
Deployment, TLS routing and restarts need their own approved target and rollback.
CI validates temporary copies with `nginx -t` and `systemd-analyze verify` on
Linux. It also starts disposable nginx and a mock backend as one unprivileged
user, forwarding a 32 KiB JSON request through an 8 KiB body buffer. This checks
the backend's 401 response with writable scratch paths and a gateway 500 with
an unwritable body directory. It does not start units, bind production listeners
or verify a host's TLS routing. The 20-minute job also uses locked dependencies.
Local macOS checks cannot run these Linux tools; inspect the exact CI or
supervisor result before relying on them.

`systemd/dots-brain.service` runs one HTTP process with private port 8765 and
OAuth-only port 8787. The ingress unit and nginx example route public traffic to
8787. Static credentials must never pass on that listener, regardless of Host.
The example gateway allows larger MCP bodies than the backend, which caps MCP
at 128 KiB and OAuth at 16 KiB. The backend remains the final limit.

## Minimal sequence for an authorized deployment

1. Privately record the exact artifact, interpreter, service owner, canonical
   directory, active clients and recovery plan. Keep the previous executable
   available; use the selected environment's executable for every command.
2. Initialize a new store or validate an authorized copy through [upgrade](../docs/upgrading.md).
3. With writers stopped, configure the intended HTTPS issuer using [OAuth](../docs/oauth.md).
4. Edit and validate the unit/gateway with the host's actual systemd/nginx tools.
   Validate scratch copies or run `nginx -t` under the gateway's service identity.
   Running `nginx -t` as root against live paths is not side-effect-free: nginx
   may create or chown its temporary directories to the default worker user
   (such as `nobody`), making them unwritable to an unprivileged gateway. Verify
   that the actual service identity can traverse and write its body and proxy
   temporary directories; syntax validation alone does not prove those permissions.
5. Start one service through that supervisor. Use `doctor --mcp-url` with an
   existing private credential; use `connect --mcp-url` for a local adapter.
6. Verify private/public authentication and the real client approval/callback.
   Include a JSON request larger than the configured nginx body buffer through
   the public route (for example, 32 KiB for the default 8/16 KiB buffer), then
   verify an authorized MCP read/write separately.
   A 401 without access is not proof of successful pairing or memory use.

Never run managed `up` beside the supervised HTTP service for the same store.
`down` does not stop an external service. Restart or stop it through its owner.
See [installation](../docs/installation.md) for exact private endpoint commands.

The optional `dots-brain-onboarding.service` controls an nginx gate only. This v2
application has no CLI onboarding-window command. When that gateway gate is used,
its operator must open it for the initiated pairing flow and then close it;
application approval still targets the exact request on the memory host.

Each changed artifact needs runtime evidence on its intended Linux host.
An external process test does not prove a real systemd unit, boot startup, TLS,
public reachability or restoration of real data. Those are separate acceptance
steps. A binary rollback cannot undo revocation, FTS changes or data mutations.
