# Uninstall and data retention

`uninstall` disables an existing instance, revokes its managed credentials, and
removes unchanged managed client entries. It preserves the database by default.

```sh
dots-brain --data-dir /absolute/private/memory uninstall --dry-run
dots-brain --data-dir /absolute/private/memory uninstall
dots-brain --data-dir /absolute/private/memory doctor
```

Run the preview first. `doctor` should then report `disabled`. A partial result
names the remaining configuration or remote-revocation work; it is not success.

`down` only stops the managed background process. Manually supervised services and
stdio clients need their own shutdown. Removing a source checkout or package is a
separate operation after data and client configuration have been identified. Do
not delete a shared environment or the canonical data directory to make removal
appear complete.

An intentional reinstall can use `up --resume`; ordinary `setup` does not remove
the disable marker. Erasure of data, exports, and backups is a separate destructive
request with a separately verified target.
