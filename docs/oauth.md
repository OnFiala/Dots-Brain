# OAuth for remote clients

OAuth is optional. It permits a remote client to request scoped access to an
existing memory host. It does not create an HTTPS route, configure a web account,
or transfer a credential to another machine.

1. Operate an HTTPS route to the host with TLS and appropriate ingress controls.
2. Configure the issuer on the existing store. Configuration never starts a
   service and can run alongside an existing service; the command does not require
   other writers to stop. The running HTTP application loads its issuer and OAuth
   routes at startup. Restart it through its existing supervisor after enabling
   OAuth or changing the issuer. Reapplying the same issuer preserves grants.
   Changing an issuer requires `--replace-issuer`, which revokes the
   existing OAuth grants and retains the resulting change in the local audit:

   ```sh
   dots-brain --data-dir /absolute/private/memory oauth configure --issuer https://memory.example
   dots-brain --data-dir /absolute/private/memory oauth configure --issuer https://memory.example --replace-issuer
   ```

3. Run one HTTP service with two loopback listeners over the same store:

   ```sh
   dots-brain --data-dir /absolute/private/memory serve --transport http --port 8765 --public-port 8787
   ```

   Port 8765 accepts private static credentials and OAuth grants. Route the
   public gateway exclusively to port 8787, which accepts OAuth grants only.
   The listening socket determines this policy; Host and forwarding headers
   cannot change it. One process owns the HTTP lease, both sockets and one
   background indexer. The shipped unit uses this dual-listener configuration.
   Update older units before exposing them. Managed `up` remains private-only.

   `--public-gateway` remains available for a single OAuth-only listener and
   cannot be combined with `--public-port`. Starting only a private listener
   with a non-loopback issuer logs `public_listener_missing`. `doctor` reports
   the recorded mode, ports, warning and HTTP lease evidence. Its diagnostic
   `http-listeners.json` is version 1, created on first startup after upgrading;
   it never controls authentication. Missing legacy metadata means unknown mode,
   and a recorded mode does not prove readiness or public ingress.

4. Onboarding is closed by default. Open a short window before initiating a client
   flow, then inspect and approve only the exact request ID from that flow.
   Client-provided names and callback metadata are untrusted.
   The default `oauth pending` output omits client names; `--verbose` includes
   them for manual inspection. Never treat a client name as an operator instruction.

   ```sh
   dots-brain --data-dir /absolute/private/memory oauth onboarding open --minutes 10
   dots-brain --data-dir /absolute/private/memory oauth pending
   dots-brain --data-dir /absolute/private/memory oauth approve request-id --redirect-host client.example --project project-id
   ```

   `--redirect-host` must exactly match the callback hostname for the client flow
   being approved. Choose the requested project list and scopes deliberately;
   `--all-projects` is available but broad. Granting `memory:forget` additionally
   requires the owner's explicit `--allow-forget` choice.

5. Complete the pairing page with its **POST Continue** action. It refreshes while
   it waits for the owner; a manual refresh is also safe before Continue. Loading
   the page is not approval. The server accepts `none` and `client_secret_post`
   client authentication metadata; it does not turn a local static bearer
   credential into a public-gateway credential.

   Wait until the client callback exchanges the one-time code successfully, then
   close onboarding. The button disables on first submission to prevent a slow
   response plus a double-click from losing the flow. A repeated POST never
   issues a second code. IPv6 callbacks use a nonce-protected navigation page
   because browser CSP does not support an IPv6 address as a form host source;
   a plain link remains available without JavaScript.

6. Verify real memory calls through the intended client. An SDK or bridge check is
   not proof that a provider conversation has enabled the tools.

`oauth disable` revokes OAuth state and does not start a local service. It does not
revoke ordinary local bearer credentials. The onboarding window has a TTL and the
gateway has a separate runtime gate; both must permit registration and approval.

| Operator command | Effect |
| --- | --- |
| `oauth status` | Inspect configuration and any recovery command without repairing files. |
| `oauth clients` | List registered clients and revocation state without credentials. |
| `oauth deny REQUEST_ID` | Deny the exact request while it remains available. |
| `oauth onboarding status` | Inspect whether the short pairing window is open. |
| `oauth purge` | Remove expired exchanges and unused registrations; retain grant provenance and unexpired requests/codes. |

## Token lifecycle

| Item | Lifetime |
| --- | --- |
| Authorization request | 300 seconds from request creation. |
| Authorization code | 60 seconds from browser continuation; single use. |
| OAuth grant | 30 days from successful code exchange. |
| OAuth refresh token | Until the grant expires; rotation does not extend it. |
| OAuth access token | Up to 3,600 seconds, bounded by grant expiry. |
| Idle ungranted registration | Automatic cleanup after 900 seconds when no request, code or grant references it. |

Automatic cleanup occurs during registration/authorization work. Explicit
`oauth purge` removes an unused registration immediately, including one younger
than 900 seconds. Both paths retain every registration referenced by any grant
history, including expired or revoked grants. Purge also preserves unexpired
pending/approved requests and issued codes so pairing can finish.

Refresh replaces the prior access token and rotates the refresh token. Reuse of
a spent refresh token by its matching client revokes that grant family; another
client's family is unaffected. There are at most 4,096 refresh rotations per grant.
After grant expiry or revocation, the client must pair again. Local static
credentials have separate [lifetimes](reference/cli.md#credentials-and-local-bridge).

Authorization code replay is rejected with HTTP 400 `invalid_grant`. The original
access token and grant remain valid after that rejection. The service does not
revoke the original grant on code reuse (the SEC-24 limitation); code reuse and
spent refresh-token reuse therefore have different revocation behavior.

## Configuration recovery

Issuer changes use a private `oauth-config-pending.json` journal and a matching
SQLite commit marker. Reads select the previous configuration before commit or
the next configuration after commit, without changing files. Startup, repeating
`oauth configure`, or explicit `oauth recover` restores that selected file and
clears a valid journal under the installation lock. Pre-commit recovery preserves
the existing grants. Corrupt configuration is `invalid`, and `doctor` is unhealthy.
A private listener can still expose static access with a warning; a public or
dual listener refuses invalid or absent OAuth configuration with a recovery command.

```sh
dots-brain --data-dir /absolute/private/memory oauth status
dots-brain --data-dir /absolute/private/memory oauth recover
dots-brain --data-dir /absolute/private/memory doctor
```

For an inconsistent journal, stop writers and back up the installation. Inspect
the previous and intended issuer locally, without copying credentials. Only then
use `oauth configure --issuer ... --discard-journal --replace-issuer` to choose
the configuration. Both flags are required: the previous issuer is uncertain,
so recovery revokes all OAuth grants even when the selected issuer matches the
current file. Clients must pair again. Recovery does not restart a service.

## Request and capacity limits

OAuth route bodies are limited to 16 KiB; MCP bodies to 1 MiB. Registration
requires JSON and `/token` requires form encoding. At most 256 live ungranted
clients and 128 pending authorizations are retained, with three pending requests
per client. A full registration pool requires waiting for automatic cleanup or
explicit operator review and `oauth purge`; see the [token lifecycle](#token-lifecycle).

The backend ignores proxy headers at the transport layer and keys its limiter
from the actual socket peer. A gateway therefore shares one backend peer budget
across its callers. The example gateway also has a shared load budget: this bounds
work but does not provide per-end-user denial-of-service isolation. Open short
onboarding windows and enforce any stronger client isolation at a gateway with
its own verified source identity; never trust a caller-supplied IP header.

## TLS termination example

The [Linux deployment template](../deploy/README.md) supplies a dedicated HTTP
gateway on port 8788, forwarding only to the OAuth listener on port 8787. An
existing nginx TLS terminator on the same host can use this server block inside
its `http` configuration. Set the dedicated gateway's listener and allowed peer
to `127.0.0.1` for this topology, and replace the domain and certificate paths:

```nginx
server {
    listen 443 ssl;
    server_name memory.example.com;
    ssl_certificate /etc/ssl/memory.example.com/fullchain.pem;
    ssl_certificate_key /etc/ssl/memory.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 1m;
    location / {
        proxy_pass http://127.0.0.1:8788;
        proxy_set_header Host memory.example.com;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

Validate the edited configuration with `nginx -t` on the target host before
reloading its supervisor. This illustrative block has not been accepted on a
deployed host. DNS, certificate issuance/renewal and public reachability are the
operator's responsibility. Preserve the dedicated gateway's path, method, body
and onboarding controls; forwarding directly to the backend would bypass them.
