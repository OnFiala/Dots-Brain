# Shared-store contract

One host owns the canonical store. Local and remote clients must point to that
store through an authorized transport. A client configuration, shell probe, or
source checkout does not establish that a second machine uses the canonical data.

Before changing an installation, record the data directory, executable version,
supervisor, active clients, and rollback source. Keep deployment identifiers,
network addresses, and client identities in a private operator record.
