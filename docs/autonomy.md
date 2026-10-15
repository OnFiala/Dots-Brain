# Autonomous setup boundaries

An agent can install Dots Brain on an authorized Linux host, configure a supported
local client, and verify the generated bridge. It must report the exact host, data
directory, version, and verified transport.

```sh
python3 scripts/bootstrap.py --data-dir /absolute/private/memory --connect codex
```

The bootstrap is idempotent for an existing enabled store. It uses the lockfile,
does not print credentials, and does not create a public endpoint. Add `--semantic`
only when local model preparation is requested.

Bootstrap invokes `up --resume`. Establish an existing store's disabled reason
before running it: only ordinary uninstall may be resumed with owner authority.
Restore and unknown recovery barriers require [recovery](upgrading.md); neither
bootstrap nor an agent may remove them to make startup succeed.

An agent must not claim that a remote device, web conversation, public ingress,
continuous capture, CORTEX endpoint, host reboot, or host-loss recovery works
without direct evidence. A missing device path is a concrete boundary, not a reason
to create another database or weaken account controls.

CORTEX and snapshot capture are disabled; there is no activation or publication
path. See [activity](activity.md) for their current boundaries and
[retained module constraints](../modules/README.md) for future activation work.
