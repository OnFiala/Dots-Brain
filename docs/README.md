# Documentation

Dots Brain shares explicitly saved memories between AI clients. It is an alpha with verified appliance and bot connections; complete provider
conversation capture remains unverified.
Start with the [project README](../README.md) and [verified capabilities](capabilities.md).

| Your task | Guide |
| --- | --- |
| Ask an agent to install, connect, or remove Brain | [Setup skill](../skills/setup/SKILL.md) |
| Install on the canonical memory host | [Installation and operation](installation.md) |
| Understand what the agent can automate | [Autonomous setup contract](autonomy.md) |
| Configure VM-local OAuth and authorize a client | [OAuth on your memory host](oauth.md) |
| Save, find, update, and forget memories | [Using memory](using-memory.md) |
| Use existing memory from an agent shell | [Shell access](shell-access.md) |
| Disconnect, uninstall, or reinstall | [Uninstall and data retention](uninstall.md) |
| Update an existing installation | [Upgrading](upgrading.md) |
| Diagnose a failed connection or setup | [Troubleshooting](troubleshooting.md) |
| Understand storage, trust, and planned integrations | [Architecture](architecture.md) |
| Review the selected appliance, CORTEX, and bot contribution requirements | [Appliance contract](appliance-contract.md) |
| Operate audit, snapshot capture, and CORTEX scopes | [Activity and connectors](activity.md) |
| Prepare the appliance service and acceptance | [Deployment and acceptance](appliance-deployment.md) |
| Review tests and measured load | [Verification](verification.md), [stress tests](stress-tests.md) |
| Review the historical shared-VM installation | [VM deployment evidence](vm-deployment.md) |
| See planned functionality | [Roadmap](roadmap.md) |
| Develop or publish a version | [Contributing](../CONTRIBUTING.md), [releases](releases.md), [changelog](../CHANGELOG.md) |

These guides cover the implemented alpha's local lifecycle and its known limits.
Public ingress and both named bot connections are verified on the appliance.
Automatic web-account setup, provider-wide conversation capture, an isolated
credential vault, scheduled off-host backups and appliance reboot acceptance
remain unfinished. Bounded snapshot collection and guarded restore do not prove
continuous capture or host-loss recovery.

Run `dots-brain --help` and `dots-brain <command> --help` for the command's complete
argument list. In a source checkout use `.venv/bin/dots-brain` on Linux. Put
`--data-dir /absolute/private/memory` before the subcommand whenever you chose a
nondefault directory; commands must address the same canonical instance.
