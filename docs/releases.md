# Releasing Dots Brain

Release only a reviewed commit already reachable from `main`. Published tags and
assets are immutable: correct an error in a new version.

1. Set the package, runtime, plugin, README, and changelog version. Run the local
   checks and commit the verified milestone.
2. Merge the reviewed commit to `main`, verify CI on the exact commit, then create
   an annotated tag from that commit, for example `v0.4.0-alpha.2`.
3. Create a draft prerelease with accurate release notes. Do not publish it yet.
4. Run the release workflow from `main` with that tag. It resolves
   `refs/tags/<tag>`, checks main ancestry, runs the locked test matrix, builds
   artifacts, and installs the wheel in an isolated environment before upload.
5. Inspect the draft assets and checksums before publishing. Do not attach memory
   databases, credentials, model artifacts, private exports, or host records.

The release workflow can upload only to an existing draft. It never publishes a
release by itself.
