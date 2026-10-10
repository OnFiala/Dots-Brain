# OAuth for remote clients

OAuth is optional. It permits a remote client to request scoped access to an
existing memory host. It does not create an HTTPS route, configure a web account,
or transfer a credential to another machine.

1. Operate an HTTPS route to the host with TLS and appropriate ingress controls.
2. Configure the issuer while writers are stopped. Configuration never starts a
   service. Changing an issuer requires `--replace-issuer`, which revokes the
   existing OAuth grants and retains the resulting change in the local audit:

   ```sh
   dots-brain --data-dir /absolute/private/memory oauth configure --issuer https://memory.example --no-start
   dots-brain --data-dir /absolute/private/memory oauth configure --issuer https://memory.example --replace-issuer --no-start
   ```

3. Onboarding is closed by default. Open a short window before initiating a client
   flow, then inspect and approve only the exact request ID from that flow.
   Client-provided names and callback metadata are untrusted.

   ```sh
   dots-brain --data-dir /absolute/private/memory oauth onboarding open --minutes 10
   dots-brain --data-dir /absolute/private/memory oauth pending
   dots-brain --data-dir /absolute/private/memory oauth approve request-id --redirect-host client.example --project project-id
   dots-brain --data-dir /absolute/private/memory oauth onboarding close
   ```

   `--redirect-host` must exactly match the callback hostname for the client flow
   being approved. Choose the requested project list and scopes deliberately;
   `--all-projects` is available but broad.

4. Complete the pairing page with its **POST Continue** action. Loading the page
   is not approval. The server accepts `none` and `client_secret_post` client
   authentication metadata; it does not turn a local static bearer credential
   into a public-gateway credential.

5. Verify real memory calls through the intended client. An SDK or bridge check is
   not proof that a provider conversation has enabled the tools.

`oauth disable` revokes OAuth state and does not start a local service. It does not
revoke ordinary local bearer credentials. The onboarding window has a TTL and the
gateway has a separate runtime gate; both must permit registration and approval.

Issuer changes use a private `oauth-config-pending.json` journal and a matching
SQLite commit marker. A file-publication failure rolls back grant invalidation;
a crash leaves OAuth disabled until `oauth configure` is run again. Reuse the
intended issuer and the existing `--replace-issuer` approval flag. Do not delete
the journal manually: it determines whether the old or new configuration committed.
