# Appliance deployment — public OAuth candidate running

## Observed boundary (2026-10-09)

The owner approved the first internal deployment on 2026-10-09. The x86_64 Linux
host `openclaw-appliance` now runs one dedicated Dots Brain systemd service with
Python 3.12.3. Its only application listener is `127.0.0.1:8787`. A separate bounded
gateway receives the owner-approved public Cloudflare route. Existing Agent
Workspace and OAuth containers remain separate services, with their original
tunnel routes preserved. Tailscale Serve returned an empty configuration during
the initial internal acceptance.

The observed installed source is `7417e7bd5528be1e85025fe6d1aa337e30dca4ff`,
including the OAuth provenance, deletion-target and useful-context repairs below.
Source archive SHA-256:
`a33274319e91203db3c97c1a31a7c01059c9397e0baa1259c3c473eb41b483da`.
Previous `2c8823b`, `a28d25f` and initial `bd7e938` deployments remain
available as rollback releases. Dependencies were installed
with the committed lockfile and `uv 0.10.3`; serving uses offline local inference.
See [live verification and its limits](verification.md).

## Current installation

| Item | Installed value |
| --- | --- |
| Service identity | Dedicated non-login `dots-brain` OS user/group |
| Immutable code | `/opt/dots-brain/releases/7417e7bd5528be1e85025fe6d1aa337e30dca4ff` with locked `.venv`, root-owned |
| Selected code | `/opt/dots-brain/current` symlink to the verified release |
| Canonical data | `/var/lib/dots-brain`, owned by service user, directory 0700 |
| Backups | `/var/backups/dots-brain`, private; off-host destination still to provision |
| Service | [`dots-brain.service`](../deploy/systemd/dots-brain.service), supervised foreground HTTP |
| Listener | Application `127.0.0.1:8787/mcp`; gateway `172.17.0.1:8788`; public `https://dots-brain.ofops.co/mcp` |
| Embeddings | Pinned quantized multilingual MiniLM, local CPU, two ONNX threads |
| Initial resource guard | Verified CPU quota 2 cores, memory high 1.5 GiB / max 2 GiB |
| External model calls | None |

The unit is active and enabled for boot. A controlled service restart retained
the test IDs/revisions. A machine reboot and power-loss recovery have not been
performed. Data directories are 0700 and the SQLite file is 0600. Initial synthetic acceptance left zero live memories and revoked its test
credentials. Subsequent bot contributions are recorded below; a read-only check
during the overnight completion review confirmed 46 live memories/revisions (38 Botter, 8 Grok).

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
restarted. OAuth is configured for `https://dots-brain.ofops.co`. The dedicated gateway is active on
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
POST was verified separately as 503.

### Owner-approved public route acceptance

The owner then explicitly approved publishing `dots-brain.ofops.co` through the
existing `agent-workspace-mcp` Cloudflare tunnel to `http://172.17.0.1:8788`.
The UI confirmed route and DNS creation; both existing Workspace routes remained
unchanged. External Mac probes verified DNS, TLS with certificate validation,
discovery/resource metadata, public-client `none`, PKCE S256, anonymous MCP 401,
unknown-path 404, closed onboarding 503 and `no-store`, without a login redirect.

Two synthetic OAuth clients then completed the real public HTTPS path through
Cloudflare, nginx and the appliance: exact owner pairing, code exchange, MCP
status/write/read, shared ID/revision, foreign-project denial and isolated grant
revocation. Cleanup deleted the synthetic record and revoked both grants. An
independent reviewer repeated bounded public discovery/auth probes; a stress
reviewer verified the 16 KiB/128 KiB body limits, methods and invalid suffix denial.
This establishes public route acceptance. Actual account evidence follows below.

### Actual bot onboarding

Grok completed OAuth with the HTTPS Cursor callback. The owner supplied its exact
pairing request ID; approval narrowed the request to `shared` and only
`memory:read memory:write`. On 2026-10-09 at 19:34:22 UTC, Grok reported status,
one synthetic write and exact-ID revision-1 readback from its real chat tools.
Independent server metadata confirmed a single matching active grant, the same
revision writer, and completed server-observed intent/receipt IDs 59/60.
Botter subsequently read this same synthetic fact; no personal import occurred.

The owner approved creation of a ChatGPT Dots Brain plugin. Its first OAuth attempt
failed before pairing: registered scope was only `memory:read`, while authorization
requested read/write (`invalid_scope`). The code now defaults omitted registration
scope to read/write eligibility, still requiring exact owner approval. Explicit
read-only registration remains restricted. Focused local tests: 26 passed, one
Linux process test excluded. All 27 OAuth tests passed on the appliance as
`nobody` with disposable fixtures; [CI on Python 3.11/3.12 also passed](https://github.com/OnFiala/Dots-Brain/actions/runs/37983273940).
The new release was selected after a verified backup at
`/var/backups/dots-brain/pre-dcr-2c8823b.sqlite3`. Only Dots Brain restarted;
independent inspection confirmed source hashes, private backup integrity and the
unchanged Grok grant.

The failed ChatGPT plugin was uninstalled. A fresh plugin, **Dots Brain Memory**,
completed OAuth with a different client and its own `shared` read/write grant.
Its pairing ID was observed in the actual browser and approved exactly. Both
active grants were confirmed through owner metadata. The old uninstalled developer
draft is retained.

Botter then reported actual chat status, exact-ID readback of Grok's revision-1
fact, one synthetic write and its own revision-1 readback. Server metadata places
that write at 2026-10-09 20:06:19 UTC, attributes its revision to Botter's separate
grant, and confirms completed server-observed intent/receipt IDs 61/62. Both grants
remain limited to `shared` and `memory:read memory:write`; onboarding is closed and
the service is active. Reads are client-reported through the owner relay; the
server audit corroborates writes, not read calls. Grok then confirmed the reverse
read of Botter's exact revision-1 fact and status showing two memories/revisions,
both indexed with no pending work. This completes bidirectional client acceptance;
it does not establish semantic query quality or complete provider capture.

At 20:15:30 UTC, owner cleanup verified both exact synthetic identities, writers,
contents and current revision 1 before deleting either. Each audited deletion used
the exact ID, project and expected revision; both subsequent reads returned not
found. A separate metadata check confirmed neither test ID remained and both live
bot grants were unchanged. Audit pairs 63/64 and 65/66 confirm the owner's two
completed deletions. The private operator report preserves the exact invocation
IDs and results; the deletion audit at that time did not itself contain the object ID.
No personal knowledge was imported during this acceptance.

The earlier deployed `2c8823b` had two auditability limitations. DBR-ONB-001: its
grants do not retain the consumed pairing request ID; current bot attribution is
correlated through unique clients/grants, revision writers and server audit.
DBR-CLEAN-001: deletion audit lacks the memory ID and observed revision; the private
operator report supplies that association for this acceptance only.

Both now have deployed repairs: additive OAuth provenance tables and allowlisted
deletion targets in intents/receipts. The upgrade below used stop, backup and
same-issuer configuration. It did not backfill old grants or rewrite audit history.

### Initial knowledge contributions

The owner relayed both bots' initial contribution receipts. Safe server metadata
confirmed **37 Botter records** written at 20:21:52–20:23:32 UTC and **8 Grok
records** at 20:24:49–20:25:09 UTC on 2026-10-09. All 45 have revision 1, project
`shared`, their respective authenticated grant writers and completed write receipts
(Botter receipts 68–140; Grok 142–156). No personal content was read for verification.

Each bot reported exact-ID readbacks and a successful Czech hybrid query with
supporting source identity. Grok reported 45 indexed records and no pending work.
These contributions are bot self-reports, not an independent audit of the truth or
completeness of their personal knowledge. Source text and topic identifiers remain
outside this repository.

Botter reported title-only output for one `memory_context` result. A synthetic
regression reproduced that behavior; repair DBR-CTX-001 supplies a bounded
title/body excerpt from the same revision without changing search ranking. It
passed the live synthetic check below. Clients should still use `memory_get` when
a bounded excerpt lacks the needed details.

Grok reports storing the lookup/save rule in its own persistent agent memory;
Botter reports only a current-chat instruction. Neither is a verified provider
hook or later-session acceptance. Live CORTEX and full provider capture remain
unverified.

### Repair deployment and preserved live data

Release `7417e7b` passed **169 tests, one model-dependent skip** on Linux Python
3.11 and 3.12 ([CI 37987652299](https://github.com/OnFiala/Dots-Brain/actions/runs/37987652299)).
The exact source archive SHA-256 was
`a33274319e91203db3c97c1a31a7c01059c9397e0baa1259c3c473eb41b483da`.
Dependencies were prepared offline in the new root-owned immutable release.

The stopped baseline contained 46 revision-1 records: Botter had added one after
its initial batch, so 38 belonged to Botter and 8 to Grok. The private backup
`/var/backups/dots-brain/pre-repair-7417e7b.sqlite3` passed integrity and foreign-key
checks. Same-issuer OAuth configuration added the companion tables. Only Dots Brain
restarted at **2026-10-09 20:36:40 UTC**; the ingress PID remained unchanged and
onboarding stayed closed. All record/revision/digest/writer metadata and the full
existing grant metadata matched the stopped baseline. Legacy provenance is null.

A separate synthetic project then exercised the actual HTTP MCP service and local
embedding model. An exact title match returned both title and body in bounded
context. MCP deletion recorded the exact ID/revision in intent/receipt 161/162.
The probe record was removed and its private credential revoked; all 46 original
records still matched the baseline. No personal text was read. An independent
reviewer verified matching critical source hashes, service identity/PID, listener
shape and private directory modes; private backup/report checks were performed by
the deploying operator because the reviewer did not elevate through that boundary.

Both bots then confirmed actual-chat post-upgrade status: 46 memories/revisions,
semantic ready, 46 indexed and zero pending. Their existing OAuth connections
remained usable. Botter repeated the originally affected query and confirmed a
useful body excerpt from the expected revision-1 source. Grok confirmed status and
retention of its eight records; a separate post-upgrade context result was not
reported. No new pairing was needed.

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

## Secure connections and remaining integration

Administration uses the existing tailnet; the application remains on loopback.
The approved Cloudflare route exposes only the constrained MCP/OAuth gateway.
Both Botter and Grok have completed their actual OAuth flows and hold separate
read/write grants for `shared`, without deletion, audit or CORTEX access. Onboarding
stays closed between approved pairing windows. The connection evidence appears above.

A configured issuer does not provision a tunnel. On this externally supervised
installation, use `oauth configure --no-start` or `oauth disable --no-start` while
the service is stopped, then restart its unit. Do not mix the managed `up` lifecycle
with systemd. Keep required path, method, source, request-size and concurrency
controls from the [ingress guide](appliance-ingress.md).

Upstream CORTEX remains separate work: provision a dedicated service identity,
verify its supported MCP endpoint and one-to-one project mapping, then approve
specific client scopes before exposing it to either bot. The existing read-only
Grok connector is not a writable service credential. No current bot grant is widened
by deploying the connector implementation.

## Audit review and outstanding operations

The owner-requested Codex heartbeat `dots-brain-audit-2-denn` is registered ACTIVE
for 09:00/21:00 in the operator's Europe/Prague timezone. App creation/view and the
local registry contract check passed. It targets this work's existing chat; the
observed current chat model is `gpt-6-astra/xhigh`. The heartbeat API/registry has
no independent model or next-run timestamp field. The first scheduled trigger at
`2026-10-09T19:22:13Z` ran, but its restricted shell could not resolve the canonical
tailnet hostname. The helper reported `incomplete/CalledProcessError`; no privilege
escalation, alternate endpoint, completion envelope or checkpoint advance occurred.
At that failed scheduled run the checkpoint remained ID 50, completed at
`18:45:04Z`. The later manual review below advanced it to 162. Persistent thread metadata
shows `gpt-6-astra`, provider `openai`, effort `xhigh`; that is configuration evidence,
not backend execution telemetry for the scheduled inference. Network access for
unattended review still needs an explicitly permitted path. This failure was not
treated as a safe audit period or silently repaired by the automation.

The initial manual [review helper](activity.md#twice-daily-operator-review) run
read all 50 then-existing canonical audit events, checked payload/hash continuity and saved its
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

### Overnight operator audit

On 2026-10-09, the operator reviewed all 112 new audit events (IDs 51–162),
including actor/project pairing, mutation results, synthetic cleanup and the
initial contributions. Every intent had a completed receipt; none was unresolved.
The local checkpoint advanced to 162 only after complete analysis. Known incomplete
provider coverage remains recorded. This was a manual run with an approved network
operation, not a successful unattended execution: the restricted default context
still failed to reach the fixed SSH origin. That availability gap remains active.

During that review, core PID 1327391 and gateway PID 1317846 were unchanged and active;
onboarding was closed. The host reported about 5.5 GiB available RAM and 190 GiB
free on the data filesystem. These are point-in-time observations, not capacity or
uptime guarantees.
