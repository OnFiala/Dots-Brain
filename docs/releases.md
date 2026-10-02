# Releasing Dots Brain

Use an existing annotated version tag and a draft GitHub release. Published tags
and assets are immutable by project policy: fix an error in a new version.

1. Update the package, runtime, plugin, README, and changelog versions. Run the
   checks in the contributing guide and commit the milestone.
2. Push the commit and an annotated tag, such as `v0.1.0-alpha.2`. Verify that CI
   passes for the tagged commit on both supported Python versions.
3. Write English release notes describing implemented behavior, validation,
   installation, and remaining limitations. Create a draft with those notes:

   ```sh
   gh release create v0.1.0-alpha.2 --verify-tag --draft --prerelease \
     --title 'Dots Brain v0.1.0-alpha.2' --notes-file /path/to/release-notes.md
   ```

4. Run the versioned release workflow from `main`:

   ```sh
   gh workflow run release.yml --ref main -f tag=v0.1.0-alpha.2
   ```

   It resolves the tag to an immutable commit, verifies both Python versions,
   builds a wheel and source distribution, packages the installer checkout, and
   creates SHA-256 checksums. The upload job has repository write access; build
   jobs do not. Assets go only to the existing draft. Retries compare existing
   assets before uploading missing files and never overwrite a differing asset.

5. Verify that the workflow succeeded, inspect the draft, download the assets,
   and run `sha256sum --check SHA256SUMS`. Confirm the release notes describe
   the exact tagged code and publish the draft:

   ```sh
   gh release edit v0.1.0-alpha.2 --draft=false
   ```

Keep alpha and beta versions marked as prereleases. The installer ZIP is a
source package with an onboarding skill; publishing it does not register or
install a ChatGPT plugin. Do not attach local databases, credentials, model
weights, private exports, or development environments.
