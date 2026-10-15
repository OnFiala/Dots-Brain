# OAuth for remote clients

OAuth grants scoped access to an existing memory host. Configuration does not
create an HTTPS route, start a service or pair a provider account. Pairing names
and callback metadata come from the client and are untrusted.

The ingress operator owns TLS, request size and rate limits, access logs and
rollback. Keep host names, internal addresses, client identities and gateway
details in a private operator record. Verify the exact public origin, approval
flow and real MCP calls through each intended client before accepting remote
access. A local HTTP test alone cannot establish that path.

## Configure offline, then start the intended listeners

Stop the HTTP and stdio servers before `oauth configure`, `oauth disable`,
`uninstall` or restore activation. Servers hold a shared writer lease for their
lifetime. Bulk maintenance requires its exclusive side and refuses a busy
installation without stopping another service. Ordinary grant revocation and
refresh rotation work online in database transactions.

```sh
dots-brain --data-dir /absolute/private/memory oauth configure --issuer https://memory.example
dots-brain --data-dir /absolute/private/memory serve --transport http --port 8765 --public-port 8787
```

The one process owns both loopback sockets, one HTTP lease and one indexer. Port
8765 accepts private static credentials and OAuth grants. Route the external
HTTPS gateway only to port 8787, which accepts OAuth grants. The policy is bound
to the socket; changing Host or forwarding headers cannot enable static access
on the public listener. `--public-gateway` creates a single OAuth-only listener;
it cannot be combined with `--public-port`. Managed `up` remains private-only.

An invalid OAuth configuration closes OAuth access at startup and during use.
It never enables static access on the public listener. The private listener can
still authenticate its static credentials. Use `oauth status` to inspect the
configuration; enabling or changing OAuth requires a supervised restart.
The compatibility `--no-start` flag is accepted; neither form starts a service.

## Owner approval and callback

Initiate the connection from the intended client, then inspect the exact pending
request on the host. The pairing page shows the client-supplied name, callback,
requested scopes and waiting state. It refreshes until the host decides.

```sh
dots-brain --data-dir /absolute/private/memory oauth pending
dots-brain --data-dir /absolute/private/memory oauth approve REQUEST_ID --project work --scope memory:read --scope memory:write
dots-brain --data-dir /absolute/private/memory oauth deny REQUEST_ID
```

The first approval or denial is final. Repeating the same decision is idempotent;
a conflicting decision is refused. Approval chooses a nonempty subset of the
requested scopes. `--all-projects` is broader than explicit project choices.
`memory:forget` also requires `--allow-forget` and explicit deletion intent when
the tool is used. The callback delivers a one-time code bound to the client,
redirect URI, resource and S256 PKCE verifier. Supported client authentication is
`none` or `client_secret_post`. The browser returns automatically after approval;
there is no separate onboarding window or POST continuation command.

Verify real memory calls in each intended assistant after pairing. An SDK test
does not prove that a new provider conversation has loaded or used the tools.

## Lifetimes and retained history

| Item | Lifetime |
| --- | --- |
| Pending request | 300 seconds from request creation. |
| Authorization code | 60 seconds from browser claim; single use. |
| Grant | 30 days from successful code exchange. |
| Access token | At most 3,600 seconds, bounded by grant expiry. |
| Refresh token | Until grant expiry; rotation does not extend it. |

Refresh rotates tokens and removes the previous access token. Reusing a spent
refresh token from its matching client revokes the grant family. Reaching 4,096
rotations also revokes the grant. Code replay is rejected without revoking the
original grant. Clients need new pairing after grant expiry or revocation.

```sh
dots-brain --data-dir /absolute/private/memory oauth grants
dots-brain --data-dir /absolute/private/memory oauth revoke GRANT_ID
```

Grant and provenance rows remain after expiry and revocation. Registration
cleanup removes expired requests, codes and unusable tokens. A client older than
one day with no live request, code or grant is retired; its noncredential metadata
and any grant history remain. Retired registrations do not occupy the 256 usable
registration slots. Clients cannot set the server's retirement marker, and it is
not returned as public registration metadata. The existing pending limit is 128.
OAuth request bodies are limited to 16 KiB; MCP request bodies to 128 KiB.

## Bulk invalidation and failure

Changing the issuer requires the explicit `--replace-issuer` choice. Reapplying
the same valid issuer preserves grants. Replacement, disable, uninstall and
restore activation retire all old client IDs, revoke grants and remove client
secrets and live exchange material. Noncredential history remains in schema v2.
Each revocation and its reason commit with the audit event in the same transaction;
audit failure rolls back the authorization change.

```sh
dots-brain --data-dir /absolute/private/memory oauth configure --issuer https://replacement.example --replace-issuer
dots-brain --data-dir /absolute/private/memory oauth disable
```

The exclusive maintenance lease covers both the database commit and config-file
publication/removal. A database failure preserves the previous grants. After a
successful database commit, a file failure never restores old access. The error
reports that the database change committed and that file publication/removal may
be uncertain. Inspect `oauth status`, retry the intended configuration operation,
restart through the existing supervisor and pair clients again. An invalid old
configuration also requires `--replace-issuer` before replacement. There is no
configuration journal or automatic recovery command.

Restore staging leaves its target disabled and reports
`disabled_pending_activation`. Activation merges and validates the source audit
before resetting OAuth access with a recovery-bound event. See
[upgrade and recovery](upgrading.md) for exact retry and rollback boundaries.

The protocol layer uses the maintained MCP SDK. Relevant primary references are
the [MCP authorization specification](https://modelcontextprotocol.io/specification/latest/basic/authorization)
and [OAuth security best current practice](https://www.rfc-editor.org/rfc/rfc9700.html).
These references do not imply acceptance of an actual deployment or assistant.
