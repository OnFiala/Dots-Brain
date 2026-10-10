# Uninstall and data retention

`uninstall` disables an existing instance, revokes its managed credentials, and
removes unchanged managed client entries. It preserves the database by default.

```sh
dots-brain --data-dir /absolute/private/memory uninstall --dry-run
dots-brain --data-dir /absolute/private/memory uninstall
dots-brain --data-dir /absolute/private/memory doctor
```

Run the preview first. `doctor` then reports `service_disabled: true` and `healthy: false`. A partial result
names the remaining configuration or remote-revocation work; it is not success.

`down` only stops the managed background process. Manually supervised services and
stdio clients need their own shutdown. Removing a source checkout or package is a
separate operation after data and client configuration have been identified. Do
not delete a shared environment or the canonical data directory to make removal
appear complete.

An intentional reinstall can use `up --resume`; ordinary `setup` does not remove
the disable marker. Erasure of data, exports, and backups is a separate destructive
request with a separately verified target.

`down` reports `external_writers_remain` when a supervised HTTP or stdio process
still holds a server lease. `doctor.checks.managed_service.writers` shows the
observed lease and Linux owner PIDs when available. A free lease is not proof
that an external program without the lease protocol has stopped.

`disconnect` preserves both credentials and registration when the client entry
was changed. Inspect that entry before retrying. An unchanged orphaned entry can
be found at the provider's default path or an explicit `--config PROVIDER=PATH`.
An unrecognized default entry is reported and retained even if the data directory
is missing. Remote credential revocation remains the issuer's responsibility.
