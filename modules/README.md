# Disabled optional source

`dots_brain_cortex` and `dots_brain_capture` preserve the existing module code
outside the core distribution. Neither directory is included in the core wheel
or source distribution. There is no module installer, discovery mechanism or
activation entrypoint.

The core never imports these packages. Its reserved `cortex` and `capture` CLI
names report `capability_unavailable` without reading old configuration or
credentials, opening module resources or creating module state. Existing state
is retained. See [activity](../docs/activity.md) and
[architecture](../docs/architecture.md) for the precise boundaries.

Module internals were moved, not repaired or accepted for activation. Internal
repairs, upstream publication and transcript import require a separate plan with
an authorized target and explicit validation of permissions, retries and recovery.

For future CORTEX activation, operation ownership follows the authenticated
principal. OAuth refresh retains it; a replacement grant creates a new operation
namespace. Republishing under a new grant can create another upstream object.
Inspect and reconcile the old publication before republishing. Stable identity
across grants needs an explicit migration design; client registration alone
does not establish it.

`tests/` retains existing characterizations with explicit synthetic module schema
and test-only MCP registration. Repository pytest runs include them; core sdist
tests do not depend on them. These tests preserve existing behavior and do not
certify the deferred module repairs.
