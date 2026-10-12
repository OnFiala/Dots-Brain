# Disabled optional source

`dots_brain_cortex` and `dots_brain_capture` preserve the existing module code
outside the core distribution. Neither directory is included in the core wheel
or source distribution. There is no module installer, discovery mechanism or
activation entrypoint in this candidate.

The core never imports these packages. Its reserved `cortex` and `capture` CLI
names report `capability_unavailable` without reading old configuration or
credentials, opening module resources or creating module state. Existing state
is retained. See [activity](../docs/activity.md) and
[architecture](../docs/architecture.md) for the precise boundaries.

Module internals were moved, not repaired or accepted for activation. The
deferred SD-X1–SD-X9, SD-C1–SD-C10 and SD-F8 work requires a separate plan with a
real target. No upstream publication or transcript import is authorized here.

`tests/` retains existing characterizations with explicit synthetic module schema
and test-only MCP registration. Repository pytest runs include them; core sdist
tests do not depend on them. These tests preserve existing behavior and do not
certify the deferred module repairs.
