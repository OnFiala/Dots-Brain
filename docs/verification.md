# Verification boundaries

Source tests, built artifacts, installed runtime, and client behavior are distinct
claims. CI validates the source tree on Python 3.11, 3.12, and 3.13 with disposable
stores. It does not establish a service on a particular host.

For a deployment, record the exact artifact, data directory, supervisor state,
backup validation, and direct read/write behavior from each intended client. Keep
that evidence private. Do not publish credentials, addresses, client IDs, or memory
contents.

The following conditions need direct acceptance evidence and remain outside a
source-only pass: public OAuth route, provider UI activation, continuous capture,
CORTEX upstream connection, host reboot, off-host recovery, semantic relevance,
and sustained capacity.
