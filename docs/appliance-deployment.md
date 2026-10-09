# Appliance deployment — internal OAuth candidate running

## Observed boundary (2026-10-09)

The owner approved the first internal deployment on 2026-10-09. The x86_64 Linux
host `openclaw-appliance` now runs one dedicated Dots Brain systemd service with
Python 3.12.3. Its only listener is `127.0.0.1:8787`. Existing Agent Workspace,
OAuth and tunnel containers remain separate services; this deployment added no
public route. Tailscale Serve returned an empty configuration during acceptance.

The installed source is `a28d25f21a7911dd5c38f78fb0f59c04b19747bd`, including the
reviewed public OAuth fixes and bounded ingress. Source archive SHA-256:
`47763505d6567dc3fef7accac4d716da686e2c170acefbb7f04dcc99f4ed5410`.
The initial `bd7e938` deployment (manifest `2026-10-09T18:20:29.347999Z`) remains
available as a rollback release. Dependencies were installed
with the committed lockfile and `uv 0.10.3`; serving uses offline local inference.
See [live verification and its limits](verification.md).

## Concrete first deployment

| Item | Installed value |
| --- | --- |
| Service identity | Dedicated non-login `dots-brain` OS user/group |
| Immutable code | `/opt/dots-brain/releases/a28d25f21a7911dd5c38f78fb0f59c04b19747bd` with locked `.venv`, root-owned |
| Selected code | `/opt/dots-brain/current` symlink to the verified release |
| Canonical data | `/var/lib/dots-brain`, owned by service user, directory 0700 |
| Backups | `/var/backups/dots-brain`, private; off-host destination still to provision |
| Service | [`dots-brain.service`](../deploy/systemd/dots-brain.service), supervised foreground HTTP |
| Listener | `127.0.0.1:8787/mcp`; no public route in this first step |
| Embeddings | Pinned quantized multilingual MiniLM, local CPU, two ONNX threads |
| Initial resource guard | Verified CPU quota 2 cores, memory high 1.5 GiB / max 2 GiB |
| External model calls | None |

The unit is active and enabled for boot. A controlled service restart retained
the test IDs/revisions. A machine reboot and power-loss recovery have not been
performed. Data directories are 0700 and the SQLite file is 0600. Acceptance left
zero live memories and revoked its synthetic client credentials.

The owner subsequently requested live Botter and Grok connections. Public route
activation and actual account acceptance are tracked separately below.

### OAuth and gateway preparation

Release `a28d25f` passed 159 tests with one real-model test skipped on the appliance,
running against disposable fixtures as `nobody`. Its Linux CI passed Python 3.11
and 3.12: [run 37978402383](https://github.com/OnFiala/Dots-Brain/actions/runs/37978402383).
The full macOS run is not a passing runtime gate: eight tests hit the Linux-only
daemon guard or sandbox socket restriction. The focused 24 OAuth tests passed there.

Before selecting the new release, a consistent private backup was written to
`/var/backups/dots-brain/pre-public-a28d25f.sqlite3` (0600). Only Dots Brain was
restarted. OAuth is configured for `https://dots-brain.ofops.co`, while the external
route has not yet been published or verified. The dedicated gateway is active on
`172.17.0.1:8788`; its [configuration and rollback](appliance-ingress.md) are separate
from the host's existing nginx service. A narrowly scoped Docker firewall exception
was necessary; the first connection correctly timed out before it was added.

Actual connector-namespace acceptance passed: discovery issuer/resource, anonymous
401, incorrect Host 404, unlisted paths and pairing suffix 404, oversized bodies 413,
unsupported method 403, two S256 owner-approved OAuth flows, real MCP status/write,
cross-client shared ID/revision, foreign-project denial, no deletion capability,
isolated revocation and registration burst 429. Direct access from the host rather
than the allowed connector namespace returned 403. The synthetic fact was deleted,
both test grants were revoked and `doctor` reported zero memories and revisions.
This establishes the internal gateway path, not either named bot's connection.

The actual ten-minute window also expired successfully: an independent reviewer
observed the unit inactive and its marker removed at 19:19:50 UTC. Supported
registration/authorization/pairing requests were then denied. The first close probe
used an unsupported GET on registration and correctly received 403; the supported
POST was verified separately as 503. Public DNS/TLS, actual bot callbacks and each
bot's shared-memory proof remain separate pending checks.

### Repeatable installation procedure (owner approval required)

1. Recheck host identity, free disk/RAM, port, root/service ownership and absence of
   another Dots instance. Record the exact commit, package hash and current unit
   state. Check available `sudo` authority; do not invent administrator access.
2. Create the dedicated user/directories. Install the exact reviewed source into
   a new release directory; use the approved `uv` executable to run
   `uv sync --frozen --extra semantic --no-dev`. Do not run Git updates in a live
   runtime directory or change existing Workspace containers.
3. As the service user, run `setup` then `model prepare` against the planned data
   directory. This explicit step downloads about 241 MiB of pinned public weights;
   the serving unit then runs offline model inference.
4. Install the reviewed unit and atomic `current` symlink, reload systemd and
   start **only** `dots-brain.service`. Use `systemctl`, not `dots-brain up`, for
   this supervised installation. Enable boot startup only after smoke acceptance.
5. Issue a short-lived project-scoped **synthetic verification** credential directly
   to a private file using `client create --url http://127.0.0.1:8787/mcp`. Verify
   SDK discovery/status and explicit write/read/revision/delete. Never print tokens.
6. Verify semantic indexing and Czech/English retrieval on the actual CPU, memory
   use, bounded load, service restart and post-restart retained IDs/revisions.
7. Create a private `backup` and stage `restore` into a disposable separate directory.
   Verify latest deletions, revocation and audit. Exercise activation only on a
   disposable pair of synthetic stores, never cut over the new live store just
   to run a test. See [recovery invariants](upgrading.md).
8. Record the instance and exact evidence. Only then connect real clients and
   import personal context. Same-disk backup is still not host-loss protection.

### Rollback

Stop/disable this new unit, preserve the data and backups, and return the `current`
symlink to the previous compatible release if one exists. On a fresh installation,
leave the service stopped and the private data retained. Do not remove unrelated
services, copy old auth grants or run a v1 binary on v2. After new writes, follow
[guarded recovery](upgrading.md); package rollback alone is not data rollback.

## Secure connections: separate next acceptance

Administration and private clients use the existing tailnet. Keep the application
bound to loopback. Any Tailscale Serve or Cloudflare route needs its own exact host,
path and authentication review before activation. The tailnet's shell connection
does not prove that either provider's remote MCP backend can reach the service.

Grok currently reports a backend HTTP connector and no verified tailnet path.
For it, provision a dedicated minimal HTTPS MCP/OAuth route on the approved tunnel;
allow only the MCP route and required OAuth discovery/registration/authorization/
token/revocation/pairing paths. Add ingress rate/body bounds without logging auth
headers, tokens, authorization codes or pairing URLs. Do not publish the database,
operator CLI or a general proxy. Dots uses its own issuer and scopes, not Workspace
tokens. For this externally supervised instance, stop the unit, run `oauth configure
--issuer <verified-origin> --no-start`, then start the unit again. Likewise use
`oauth disable --no-start` with the supervisor; omit the managed `up` lifecycle.
Inspect actual OAuth callbacks during the real connection; do not relax
registration based on a historical forum report.

Botter may support Secure MCP Tunnel, but account availability is unverified.
Use supported plugin/OAuth setup and owner consent. Give each bot its own project
and capability grant; start with a synthetic cross-bot shared-project test.
Provision a separate upstream CORTEX service credential and supported MCP endpoint;
the existing read-only Grok endpoint is not sufficient evidence of write access.

## Audit review and outstanding operations

The owner-requested Codex heartbeat `dots-brain-audit-2-denn` is registered ACTIVE
for 09:00/21:00 in the operator's Europe/Prague timezone. App creation/view and the
local registry contract check passed. It targets this work's existing chat; the
observed current chat model is `gpt-6-astra/xhigh`. The heartbeat API/registry has
no independent model or next-run timestamp field. Actual scheduled execution and
its effective model remain unverified until the first run.

The manually exercised [review helper](activity.md#twice-daily-operator-review)
read all 50 canonical audit events, checked payload/hash continuity and saved its
private operator checkpoint. A resumed scan verified the same anchor and no new
events. Three reviewers accepted its repaired replay/state boundaries and 20
targeted regression tests passed. This is canonical live audit access, not a
development-database schedule. The Mac and Codex app must remain available;
there is no independent missed-run monitor. The smallest schedule rollback is
pausing that exact heartbeat through the app; it does not stop the appliance.

Configure scheduled backups and an approved encrypted off-host destination, with
retention and restore verification. Select a persistent provider capture source
before enabling a collector schedule. Neither a systemd template nor stale bot
snapshot files prove unattended operation or complete capture.

## Embedding and optional Cerebras decision

Use the already pinned and exercised ONNX MiniLM for this first CPU deployment.
It has no paid inference dependency or model-license login step. Measure quality
and resources before replacing it. EmbeddingGemma/EmbeddingGemma 2 remain candidates
for a separate fixed bilingual evaluation; their runtime/weights are not a drop-in
FastEmbed model in this candidate. This is a deployment practicality decision, not
a claim that MiniLM has better retrieval quality.

Cerebras can later propose sourced facts from authorized excerpts, classify action
batches for the senior audit model, or flag possible conflicting/stale statements.
Its structured-output API can support bounded proposals; validate the full schema
and resource limits locally. It must have no canonical write/delete authority,
no secrets or private reasoning in requests, a fixed allowlisted endpoint/model,
timeout/retry/cost caps and explicit data-egress approval. Failure must preserve
local writes/search and must not suppress the full audit evidence. No Cerebras API
credential has been provisioned or used.

Primary references checked during design:
[MiniLM model](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2),
[EmbeddingGemma](https://ai.google.dev/gemma/docs/embeddinggemma),
[Cerebras structured outputs](https://inference-docs.cerebras.ai/capabilities/structured-outputs).
