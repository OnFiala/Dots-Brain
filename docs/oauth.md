# OAuth for remote clients

OAuth is optional. It permits a remote client to request scoped access to an
existing memory host. It does not create an HTTPS route, configure a web account,
or transfer a credential to another machine.

1. Operate an HTTPS route to the host with TLS and appropriate ingress controls.
2. Configure the issuer while writers are stopped. Configuration never starts a
   service. Changing an issuer requires `--replace-issuer`, which revokes the
   existing OAuth grants and retains the resulting change in the local audit:

   ```sh
   dots-brain --data-dir /absolute/private/memory oauth configure --issuer https://memory.example
   dots-brain --data-dir /absolute/private/memory oauth configure --issuer https://memory.example --replace-issuer
   ```

3. Run the HTTP backend behind the public route with `serve --transport http
   --public-gateway`. This listener accepts OAuth grants only. Static local
   credentials work in private mode, such as managed `up`. Only one HTTP service
   may own a store: stop the public service before testing private mode.
   Request headers cannot change either policy. The shipped systemd unit enables
   public gateway mode; an existing unit must be updated before exposing this
   version. The old `--no-start` option was removed: configuration never starts a
   service, and the operator must restart its supervisor after changing OAuth.

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
   close onboarding. A repeated Continue never issues a second code; return to
   the client if the first redirect has already completed.

6. Verify real memory calls through the intended client. An SDK or bridge check is
   not proof that a provider conversation has enabled the tools.

`oauth disable` revokes OAuth state and does not start a local service. It does not
revoke ordinary local bearer credentials. The onboarding window has a TTL and the
gateway has a separate runtime gate; both must permit registration and approval.

Issuer changes use a private `oauth-config-pending.json` journal and a matching
SQLite commit marker. Reads and startup never repair or delete it. They permit
OAuth only when the journal, marker, and current configuration agree. Repeating
`oauth configure` clears an unambiguous journal while holding the installation
lock. An unreadable or inconsistent journal leaves OAuth unavailable; a private
listener can still use local static credentials.

For an inconsistent journal, stop writers and back up the installation. Inspect
the previous and intended issuer locally, without copying credentials. Only then
use `oauth configure --issuer ... --discard-journal --replace-issuer` to choose
the configuration. Both flags are required: the previous issuer is uncertain,
so recovery revokes all OAuth grants even when the selected issuer matches the
current file. Clients must pair again. Recovery does not restart a service.
