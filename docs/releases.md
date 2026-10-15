# Releasing Dots Brain

Release only a reviewed commit already reachable from `main`. Published tags and
assets are immutable: correct an error in a new version. Release publication and
deployment require explicit maintainer authority.

1. Set the package, runtime, plugin, README and changelog version. Run the
   [source and installed-wheel checks](verification.md), then commit the milestone.
2. Merge the reviewed commit to `main`, inspect CI on that exact commit, and create
   an annotated version tag from it. Before dispatch, independently verify the
   tag, its commit and main ancestry in a clean checkout of that commit:

   ```sh
   RELEASE_TAG=v0.4.3
   git fetch origin main --tags
   test "$(git cat-file -t "refs/tags/$RELEASE_TAG")" = tag
   RELEASE_COMMIT=$(git rev-parse "refs/tags/$RELEASE_TAG^{commit}")
   git merge-base --is-ancestor "$RELEASE_COMMIT" origin/main
   test "$(git rev-parse HEAD)" = "$RELEASE_COMMIT"
   ```

3. Create a draft prerelease with accurate notes. Run
   [Prepare release](../.github/workflows/release.yml) from `main` with that tag.
   It resolves the annotated tag to a commit, compares the plugin version with
   the tag, and runs the Python 3.11/3.12 checks and builds on that commit.
4. Verify the exact built wheel in a fresh environment using
   [the installed-wheel procedure](verification.md#installed-wheel). Main ancestry
   and isolated wheel installation are manual gates; the workflow does not
   enforce them. Preserve their evidence with the artifact hashes.
5. Inspect the draft assets, SHA256SUMS and notes before authorizing publication.
   Do not attach databases, credentials, model files, private exports or host records.

The build job starts with `uv sync --frozen --all-extras`: it uses `uv.lock`
without checking whether it matches project metadata. Subsequent `uv run` steps
use their normal lock behavior. The local `uv sync --locked --all-extras` gate
checks that the lockfile is current; do not treat `--frozen` as that assertion.

The workflow builds a wheel, sdist and source/plugin ZIP, verifies checksums and
uploads only to an existing draft. It checks that the tag object has not changed
before upload and compares existing assets rather than overwriting them. It does
not publish a release or deploy a host. Review the checksums and installed-wheel
result for the uploaded wheel before publishing.
