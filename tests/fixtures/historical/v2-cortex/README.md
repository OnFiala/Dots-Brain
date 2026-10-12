# Historical optional ledger

`schema.sql` is the unchanged CORTEX extension SQL from checkpoint
`68be8512806c557459628f1b55ba1f6ae9b36bf7`, originally
`src/dots_brain/cortex_connector.py:SCHEMA_SQL`.

Core recovery tests explicitly create this historical fixture to prove that an
existing ledger survives separation. Fresh core setup and released-v1 migration
do not install it. This file is test input, not a core schema or migration.
