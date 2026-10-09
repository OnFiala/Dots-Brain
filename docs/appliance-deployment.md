# Appliance deployment plan — candidate, not installed

## Observed boundary (2026-10-09)

The selected appliance is a reachable owner-operated x86_64 Linux machine with
systemd, Python 3 and Tailscale. Port 8787 was free in the read-only inspection.
Existing Agent Workspace, OAuth and tunnel containers belong to another service;
their data, credentials and listener are not Dots Brain. No production state was
changed during preparation of this candidate.

## Concrete first deployment

| Item | Planned value |
| --- | --- |
| Service identity | Dedicated non-login `dots-brain` OS user/group |
| Immutable code | `/opt/dots-brain/releases/<verified-commit>` with locked `.venv` |
| Selected code | `/opt/dots-brain/current` symlink to the verified release |
| Canonical data | `/var/lib/dots-brain`, owned by service user, directory 0700 |
| Backups | `/var/backups/dots-brain`, private; off-host destination still to provision |
| Service | [`dots-brain.service`](../deploy/systemd/dots-brain.service), supervised foreground HTTP |
| Listener | `127.0.0.1:8787/mcp`; no public route in this first step |
| Embeddings | Pinned quantized multilingual MiniLM, local CPU, two ONNX threads |
| Initial resource guard | CPU quota 2 cores, memory high 1.5 GiB / max 2 GiB; verify on host |
| External model calls | None |

Installing the service is a production change and requires the owner's actual
deployment approval. The source implementation approval is not silently treated
as permission to reconfigure existing infrastructure.

### Procedure after approval

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
tokens. Inspect actual OAuth callbacks during the real connection; do not relax
registration based on a historical forum report.

Botter may support Secure MCP Tunnel, but account availability is unverified.
Use supported plugin/OAuth setup and owner consent. Give each bot its own project
and capability grant; start with a synthetic cross-bot shared-project test.
Provision a separate upstream CORTEX service credential and supported MCP endpoint;
the existing read-only Grok endpoint is not sufficient evidence of write access.

## Audit review and outstanding operations

After the endpoint and audit access work, create the requested twice-daily Codex
review at 09:00/21:00 Europe/Prague using the strong selected model; follow
[review rules](activity.md). The automation must read sanitized, paginated audit
from the canonical appliance and report unavailable/stale coverage explicitly.
No automation has been created against the disposable development database.

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
