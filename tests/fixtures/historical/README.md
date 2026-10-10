# Historical upgrade fixtures

These databases contain synthetic test records and publicly known test tokens.
They have never been used by a running installation outside disposable tests.
Do not replace them with copies of a user's database.

`v1-263c386` was created by release `v0.3.0-alpha.2`, commit
`263c38617aa17ae1ebaea84c8938065c131cfae1`. `v2-c963700` was created by
`c9637003f365f184751a3825013b2c425efd1517`. Each manifest records the generated
file hashes and record identities. Tests verify those hashes before copying.

To regenerate intentionally, export that commit to an empty scratch directory,
then run the generator with the historical source first on `PYTHONPATH`:

```sh
PYTHONPATH=/absolute/scratch/old/src python tests/fixtures/historical/generate.py COMMIT /absolute/new-fixture
```

The generator invokes the historical setup, remember, forget, client creation,
and OAuth configuration functions. It replaces the generated probe token with a
public test value and inserts a synthetic OAuth grant through the historical
schema. The WAL test creates a committed historical-format WAL in a subprocess
that exits without checkpointing; no live process or WAL file is shipped here.

The Linux CI also runs the pinned historical code against migration backups and
confirms that the old writer rejects schema v3. Other tests need no Git access.
