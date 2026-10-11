# OAuth for remote clients

OAuth is optional. It permits a remote client to request scoped access to an
existing memory host. It does not create an HTTPS route, configure a web account,
or transfer a credential to another machine.

1. Operate an HTTPS route to the host with TLS and appropriate ingress controls.
2. Configure the issuer on the existing store. Configuration never starts a
   service. Stop the supervisor for a planned issuer change and restart it after
   configuration; the command itself does not require other writers to stop.
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
   `--all-projects` is available but broad.

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
per client. `oauth purge` removes unused clients while preserving unexpired
requests, issued codes and grant provenance. Expired exchanges are also cleaned
as new requests arrive. A full registration pool requires waiting for expiry or
explicit operator review and `oauth purge`.

The backend ignores proxy headers at the transport layer and keys its limiter
from the actual socket peer. A gateway therefore shares one backend peer budget
across its callers. The example gateway also has a shared load budget: this bounds
work but does not provide per-end-user denial-of-service isolation. Open short
onboarding windows and enforce any stronger client isolation at a gateway with
its own verified source identity; never trust a caller-supplied IP header.
