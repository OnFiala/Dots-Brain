# Shared Work/Dot VM deployment

On 2026-10-05 the owner confirmed that Work and their Dot use the same managed
Linux VM. A retained instance was then installed there. The released server is
`0.3.0-alpha.2`, commit `263c38617aa17ae1ebaea84c8938065c131cfae1`.
Private instance files and credentials are not included in this repository.

## Installation and evidence

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

The current environment reports an enforced unrestricted HTTP policy. Its network
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
