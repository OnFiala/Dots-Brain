# Releasing Dots Brain

Release only a reviewed commit already reachable from `main`. Published tags and
assets are immutable: correct an error in a new version. Release publication and
deployment require explicit maintainer authority.

1. Set the package, runtime, plugin, README and changelog version; update the
   project entry in `uv.lock` with `uv lock`. The project validator checks all six.
   Run the
   [source and installed-wheel checks](verification.md), then commit the milestone.
2. Merge the reviewed commit to `main`, inspect CI on that exact commit, and create
   an annotated version tag from it. Before dispatch, independently verify the
   tag, its commit and main ancestry in a clean checkout of that commit:

   ```sh
   RELEASE_TAG=v0.4.4
   git fetch origin main --tags
   test "$(git cat-file -t "refs/tags/$RELEASE_TAG")" = tag
   RELEASE_COMMIT=$(git rev-parse "refs/tags/$RELEASE_TAG^{commit}")
   git merge-base --is-ancestor "$RELEASE_COMMIT" origin/main
   test "$(git rev-parse HEAD)" = "$RELEASE_COMMIT"
   ```

3. Create a draft prerelease with accurate notes. Run
   [Prepare release](../.github/workflows/release.yml) from `main` with that tag.
   It resolves the annotated tag to a commit, verifies its ancestry against
   `origin/main`, compares the plugin version with the tag, and runs the Python
   3.11/3.12 checks and builds on that commit.
4. Inspect the fresh installed-wheel result from both release matrix jobs. Each
   job uses hash-locked core dependencies in a new environment and runs
   [the installed-wheel helper](verification.md#installed-wheel). Preserve the
   wheel SHA256, Python/SQLite versions and result with the candidate's evidence.
   Confirm that the uploaded Python 3.12 wheel has that same hash.
5. Inspect the draft assets, SHA256SUMS and notes before authorizing publication.
   Do not attach databases, credentials, model files, private exports or host records.

The build job starts with `uv sync --locked --all-extras`, which rejects stale
locked metadata. Source checks use that environment; wheel acceptance uses a
separate core-only environment. A failed check prevents draft asset upload.

The workflow builds a wheel, sdist and source/plugin ZIP, verifies checksums and
uploads only to an existing draft. It checks that the tag object has not changed
before upload and compares existing assets rather than overwriting them. It does
not publish a release or deploy a host. Review the checksums and installed-wheel
result for the uploaded wheel before publishing.

The source version in README identifies that checkout. Only a published release
with its tag and verified assets establishes an available versioned download;
an open pull request or passing CI does not publish it. Publication does not
upgrade an existing installation. Follow [upgrading](upgrading.md) for a separately
authorized deployment and keep its prior executable and recovery evidence.
