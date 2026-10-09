# Shared Work/Dot VM deployment

On 2026-10-05 the owner confirmed that Work and their Dot use the same managed
Linux VM. A retained instance was then installed there. The released server is
`0.3.0-alpha.2`, commit `263c38617aa17ae1ebaea84c8938065c131cfae1`.
Private instance files and credentials are not included in this repository.

## Current availability: 2026-10-09

The owner relayed read-only checks from both the bot's shell and its cloud desktop.
Direct checks of the historical instruction file, wrappers, runtime directory,
database, deployment manifest, and their shared installation parent returned
`ENOENT` in both views. This was an exact-path check, not only a filename search.
Private paths, host identifiers, credentials, and memory content remain excluded
from this repository. These are owner-relayed observations, not a live inspection
by the repository auditor.

The shell and desktop shared a hostname and workspace directory but had different
filesystem and network namespaces. Neither hostname equality nor a listening port
establishes access to the same runtime. No current Dots Brain service, safe existing
client, or successful `memory_status` call was identified in either view.

The historical installation is unavailable in the inspected environment. These
checks cannot distinguish VM replacement, an unattached original disk, or removal;
they do not establish that the original memories were erased. Recover the original
task environment or storage before treating a new empty installation as a restore.

The intended deployment still keeps memory, embeddings, authentication, and runtime
on the bot's VM without additional operating costs. The platform must establish
which files survive environment replacement, how those files can be recovered,
how the service resumes, and which supported public route reaches it. Current
[Work Cloud documentation](https://learn.chatgpt.com/docs/enterprise/chatgpt-work-cloud-security#where-cloud-tasks-run)
describes environments that can be reused or replaced while preserving eligible
state; it does not identify this installation directory as durable or document
its recovery. That general documentation is not proof of what happened to this
particular instance. No alternative memory host has been selected.

## Historical installation and evidence: 2026-10-05

- Extracted the tagged release into an isolated runtime outside the development
  checkout. The packaged bootstrap installed its locked semantic dependencies.
- Initialized one private canonical store outside the Git repository and prepared
  the pinned model with integrity verification. Inference uses the VM's CPU.
- Started authenticated loopback HTTP and verified the generated stdio bridge's
  actual write/read/cleanup sequence. Synthetic verification records were removed.
- Indexed three synthetic English records. A Czech food question retrieved the
  expected allergy record first, through the live server's hybrid search.
- Configured local OAuth discovery at the actual loopback origin. This establishes
  local configuration, not an externally reachable OAuth service.
- Installed the [source shell client](shell-access.md) with an owner-private
  wrapper. It discovers five permitted memory tools and performs real MCP calls;
  its read/write credential does not grant deletion.
- Saved an operational note with source metadata, stopped the managed process,
  and retrieved that note through the shell client. It restarted the same service
  against the same database. This is process recovery, not a VM reboot test.
- Added private workspace instructions describing the canonical instance and
  existing commands so subsequent agent work can reuse the installation.

The semantic service used approximately 688 MiB RSS after the three-record
retrieval check. This is one observed working set, not a peak or capacity promise.
The data directory had mode `0700` and the database `0600`. The local OS owner
can still read credential files; strong secret isolation is not established.

## Public connectivity remains unresolved

The October 5 environment reported an enforced unrestricted HTTP policy. Its network
snapshot separately reports no configured TCP destinations and no VPN. A targeted
CONNECT probe for a documented Cloudflare Tunnel destination on port 7844 could
not reach the advertised TCP proxy: the connection to `proxy:8088` was refused.
No tunnel credential was requested, printed, or tested.

This result does not prove that every possible tunnel is incompatible or that
credentials are missing. It establishes that this particular configured transport
is not ready. No supported public-ingress provisioning tool was available in the
session. A working public HTTPS route and actual external client still need to be
provisioned and verified. No store or authentication service was moved to Sites.

The installed local shell client does not register native tools in an already
running ChatGPT/Codex conversation. External application activation, automatic
capture, history import, VM boot supervision, VM replacement durability, and host
billing guarantees remain separate unverified capabilities.
