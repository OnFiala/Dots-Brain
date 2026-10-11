# Released v1 upgrade fixture

`v1-263c386` contains only synthetic test records and publicly known test tokens. It was generated using release `v0.3.0-alpha.2`, commit `263c38617aa17ae1ebaea84c8938065c131cfae1`, and has never been used outside disposable tests. Its manifest binds every database/configuration file by SHA256. Tests verify the hashes before copying. Never replace it with a user database.

To regenerate intentionally, export the pinned release to an empty scratch directory and run:

```sh
PYTHONPATH=/absolute/scratch/old/src python tests/fixtures/historical/generate.py 263c38617aa17ae1ebaea84c8938065c131cfae1 /absolute/new-fixture
```

The generator exercises historical setup, remember, forget, client creation and OAuth configuration. The fixture contains no WAL sidecar; committed WAL is generated separately by tests.
