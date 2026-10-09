# OAuth on your memory host

Dots Brain can run its own OAuth authorization service on the same VM and SQLite
database as your memory. It uses the official MCP Python SDK for discovery,
registration, authorization-code/PKCE validation, token responses, and revocation.
It does not require a paid identity service, hosted database, or inference API.

The candidate verifies the full flow with the official MCP OAuth client against
a live local HTTP service. The appliance's public HTTPS gateway also passed
synthetic OAuth and real MCP read/write acceptance. Both Botter and Grok verified
actual chat read/write calls using separate owner-approved grants. OAuth support
alone does not create a tunnel or give an agent access to a user's browser or
another device.

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
accepted only for `localhost` and `127.0.0.1` during local development. IPv6 HTTP
issuers are rejected because the current SDK does not support them. A configured
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
grant requires a new identified connection flow. Spent refresh-token hashes remain
until grant expiry. Reuse by their owning client revokes the entire grant, including
its replacement tokens. Clients must serialize refresh attempts; a racing duplicate
also revokes the grant and requires a new identified connection flow.
A grant permits at most 4,096 refresh rotations. Reaching this storage bound also
revokes it and requires new owner consent; spent hashes are never discarded early.

```sh
dots-brain --data-dir /absolute/private/memory oauth grants
dots-brain --data-dir /absolute/private/memory oauth revoke GRANT_ID
dots-brain --data-dir /absolute/private/memory oauth disable
```

The first two commands list metadata and revoke one authorization. An unknown grant
ID is an error. They never return token values. The standard OAuth `/revoke` endpoint also invalidates a
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
- Registrations that omit `scope` default to eligibility for `memory:read` and
  `memory:write`. Registration itself grants no access: an identified owner
  decision must still authorize each request and its projects. Explicit read-only
  registrations remain read-only. Clients registered under the previous read-only
  default need a new registration to request writes; existing grants are unchanged.
  Public clients and confidential
  `client_secret_post` clients are tested. Discovery explicitly advertises `none`
  for public clients; registration responses are marked `no-store`. The SDK's Basic method also requires
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

### Grok Bot compatibility investigation: 2026-10-09

Disposable local handler probes accepted public-client registration without a
client secret for the HTTPS Cursor callback and `http://localhost:8787/callback`
(`201`). Adding `cursor://anysphere.cursor-mcp/oauth/callback` to the same list
rejected the whole registration (`400 invalid_redirect_uri`). S256 authorization
reached owner pairing; `plain` returned `invalid_request` through the callback.
These probes used synthetic data and did not connect a real Grok Bot client.

A [September Cursor support report](https://forum.cursor.com/t/grok-bot-custom-mcp-oauth-fails-before-sign-in-redirect-uri-not-allowed/171877)
describes that three-callback registration and a planned change. The current
client's callback list was unverified at that stage. Do not relax callback validation based
only on that historical report. During an authorized real connection, inspect
sanitized registration metadata for `redirect_uris` and
`token_endpoint_auth_method`; inspect the authorization request for
`code_challenge_method`. The latter is not a registration field. Do not record
secrets, authorization codes, tokens, or pairing URLs.

Actual Grok onboarding later succeeded with the HTTPS Cursor callback and a
separate owner-approved `shared` read/write grant. Real chat status, write and
readback were confirmed by the owner relay and server writer/audit metadata.
ChatGPT's first real connection registered read-only eligibility and then requested
read/write, which correctly failed with `invalid_scope`. The omitted-scope default
above fixes that registration contract. After deployment, a fresh ChatGPT
registration completed owner-approved OAuth. Botter then reported actual chat
status, readback of Grok's fact, and its own synthetic write/readback, corroborated
by the separate writer and server audit. Grok subsequently confirmed the reverse
read, completing bidirectional client acceptance. Owner cleanup removed both
synthetic facts while preserving the grants. See [deployment evidence](appliance-deployment.md).

The OAuth tables are additive to the existing memory schema. See
[upgrading](upgrading.md), [uninstall](uninstall.md), and [troubleshooting](troubleshooting.md).

### Pairing provenance in the candidate

Owner `oauth grants` output now includes `request_id`, `request_created_at` and
grant `created_at` timestamps. The original request ID/time is copied atomically
through the one-time code into grant metadata; refresh and revocation preserve it.
Consumed codes and their temporary metadata are removed. No token, authorization
code, PKCE material or callback is added to the grant report.

Legacy grants and flows created during a rollback have `null` where the origin
was not recorded. The code does not invent their history. Existing installations
need the explicit [OAuth provenance extension](upgrading.md#oauth-provenance-extension-within-schema-v2).
This source repair has not yet been deployed to the live appliance.


## Externally supervised installations

When systemd or another supervisor owns the HTTP process, stop that service and
use `oauth configure --issuer <verified-origin> --no-start` or
`oauth disable --no-start`. Then restart through the same supervisor. The result
reports `restart_required`; these commands do not launch the managed background
process. Without `--no-start`, the original managed `up` lifecycle is retained.
Never run both lifecycle managers against one installation.

For the appliance's bounded Cloudflare gateway and short onboarding windows, see
[appliance ingress](appliance-ingress.md).
