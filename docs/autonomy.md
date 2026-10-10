# Autonomous setup boundaries

An agent can install Dots Brain on an authorized Linux host, configure a supported
local client, and verify the generated bridge. It must report the exact host, data
directory, version, and verified transport.

```sh
python scripts/bootstrap.py --data-dir /absolute/private/memory --connect codex
```

The bootstrap is idempotent for an existing enabled store. It uses the lockfile,
does not print credentials, and does not create a public endpoint. Add `--semantic`
only when local model preparation is requested.

An agent must not claim that a remote device, web conversation, public ingress,
continuous capture, CORTEX endpoint, host reboot, or host-loss recovery works
without direct evidence. A missing device path is a concrete boundary, not a reason
to create another database or weaken account controls.

CORTEX operation ownership follows the authenticated principal. OAuth token
refresh retains that principal; a replacement grant creates a new operation
namespace. Republishing under a new grant can create another upstream object.
Use the local owner's operation inspection and reconcile the old publication
before republishing. Stable bot identity across separate grants needs an explicit
identity migration design; client registration alone does not establish it.
