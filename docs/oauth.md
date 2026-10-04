# OAuth on your memory host

Dots Brain can run its own OAuth authorization service on the same VM and SQLite
database as your memory. It uses the official MCP Python SDK for discovery,
registration, authorization-code/PKCE validation, token responses, and revocation.
It does not require a paid identity service, hosted database, or inference API.

This release verifies the full flow with the official MCP OAuth client against
a live local HTTP service. Public HTTPS deployment and actual ChatGPT/Claude web
account setup have not been verified. OAuth support does not create a tunnel or
give an agent access to a user's browser or another device.

## What an agent can do

The intended request is: "Connect this AI tool to my existing Dots Brain. Use the
setup skill, keep the data on my memory host, and verify the connection."

Given an existing reachable HTTPS route and an authorized way to operate the
client, the agent configures OAuth, starts that client's connection flow, and
authorizes the exact request it initiated. Tokens travel directly between the
client and server; they are not returned by the owner CLI or copied into chat.
Client-specific account consent or missing browser/device access remains visible.

## Configure the existing host

First establish the real stable HTTPS origin through the host's supported ingress.
Do not invent a URL, disable platform network restrictions, or move the memory
to another host. The example domain below is a placeholder, not a working endpoint.

```sh
dots-brain --data-dir /absolute/private/memory oauth configure \
  --issuer https://your-real-memory-host.example
dots-brain --data-dir /absolute/private/memory oauth status
```

The command requires an existing initialized store. It adds OAuth tables to that
database, configures discovery, and starts or restarts the managed service as
needed. It does not create another memory or change the listener from loopback.
Existing local bearer connections continue working. Repeating the same issuer
preserves approvals; changing the issuer revokes previous OAuth clients and grants.

The reverse proxy or tunnel must forward `/mcp`, `/.well-known/*`, `/authorize`,
`/token`, `/register`, `/revoke`, and `/oauth/pair/*` to the same loopback listener.
Preserve the canonical public Host header or use the local upstream host. Do not
log authorization headers, token request bodies, authorization codes, or pairing
URLs. TLS terminates at the supported ingress; configure its request/rate limits.
Do not expose the loopback HTTP listener directly to the Internet.

Origins with credentials, queries, fragments, or paths are rejected. HTTP is
accepted only for exact loopback hostnames during local development. A configured
issuer is reported with `public_ingress: not_verified`; a local readiness check
does not certify that the external route reaches this VM.

## Authorize one identified connection

Point the MCP-compatible client at `https://your-real-memory-host.example/mcp`.
Its OAuth flow registers a client and opens a short pairing page. The page gives
the request ID and waits up to five minutes for the local owner agent.

```sh
dots-brain --data-dir /absolute/private/memory oauth pending
dots-brain --data-dir /absolute/private/memory oauth approve REQUEST_ID \
  --project my-project --scope memory:read --scope memory:write
```

**Approve only the exact request ID observed in the client flow you initiated.**
Client names, callback URLs, and other registration metadata are untrusted input;
a familiar name or the first pending request is not proof that it belongs to the
user. There is no public approval API and no approve-all option. Operating the
owner CLI requires access to the existing VM and its private files.

The agent can read the request ID from the pairing page when it has authorized
browser access. If it cannot see the user's flow, the user must identify that
specific request. Do not replace that missing capability with blind approval.
This fallback exchanges a request ID, not a bearer token or client secret.

The agent chooses the authorized projects; repeat `--project` for multiple
projects. `--all-projects` is a separate explicit alternative. `--scope` can only
narrow what the client requested. By default, approval grants the requested read
and write scopes and excludes deletion. Some OAuth clients request every
advertised scope automatically, so a request for `memory:forget` is not itself
permission to delete. Granting it requires `--allow-forget` and explicit user
authorization. A read-only client stays read-only.

After approval, the browser returns automatically to the registered callback.
The client exchanges the single-use code with its PKCE verifier. The agent should
then verify real MCP reads/writes and the application's own connection state.
Approval alone is not evidence that the client finished or loaded the tools.
To reject an unexpected request, use `oauth deny REQUEST_ID`.

## Renewal, revocation, and removal

Access tokens last at most one hour. Refresh tokens rotate on use and retain the
grant's original expiry, at most 30 days. Refresh invalidates the prior access and
refresh tokens, cannot add scopes, and cannot change project access. An expired
grant requires a new identified connection flow. Previously used refresh tokens
are rejected; reuse does not automatically revoke the replacement token family.

```sh
dots-brain --data-dir /absolute/private/memory oauth grants
dots-brain --data-dir /absolute/private/memory oauth revoke GRANT_ID
dots-brain --data-dir /absolute/private/memory oauth disable
```

The first two commands list metadata and revoke one authorization. They never
return token values. The standard OAuth `/revoke` endpoint also invalidates a
grant's access and refresh tokens when called by their owning client.
`oauth disable` revokes all OAuth registrations/flows/grants and restarts the
service without OAuth; existing local bearer connections remain available.

Whole-instance `uninstall` removes pending OAuth flows and revokes all OAuth
grants as well as local credentials. Reinstall cannot reactivate the old refresh
tokens. Memory, revisions, and deletion suppression remain intact. Client account
entries may still need removal through that client's supported controls.

## Boundaries and compatibility

- One owner per memory instance. This is not a shared multi-tenant identity system.
- Client metadata and optional confidential-client secrets live in the private
  SQLite database. Authorization codes and access/refresh tokens are stored only
  as hashes. The OS owner can read private files; this is not an isolated vault.
- S256 PKCE, exact registered callbacks, resource binding, expiry, single-use code
  exchange, and scoped project grants are enforced. HTTPS and exact loopback HTTP
  callbacks are supported; custom URI schemes are not supported in this alpha.
- Both authorization and token requests must carry the canonical `/mcp` resource.
  SDK 1.30 parses but does not enforce token-request resource binding; the adapter
  adds that check before invoking its handler. It also supplies the optional empty
  public-client secret field expected by that SDK version's revocation parser.
- Dynamic registrations default to read scope. Public clients and confidential
  `client_secret_post` clients are tested. The SDK's Basic method also requires
  `client_id` in the form; Basic clients without that field are not supported here.
- OAuth bodies are capped at 16 KiB, stored client metadata at 8 KiB, callbacks at
  eight per client, registrations at 256, and pending flows at 128. Expired flows
  and expired grants are cleaned during registration; unused registrations age out
  after one day. These storage bounds do not replace ingress abuse protection.
- OAuth endpoint CORS follows the SDK. Direct cross-origin browser JavaScript MCP
  access is not configured; web products that connect from their servers are a
  separate integration to verify. No named web provider is certified by SDK tests.
- Continuous capture, history import, native ChatGPT views, VM persistence, and
  automatic tunnel provisioning remain separate capabilities.

The OAuth tables are additive to the existing memory schema. See
[upgrading](upgrading.md), [uninstall](uninstall.md), and [troubleshooting](troubleshooting.md).
