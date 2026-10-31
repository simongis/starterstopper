# Releasing

StarterStopper uses [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **PATCH** (1.0.1): bug fixes only.
- **MINOR** (1.1.0): new features that don't break existing configs (a new provider, a new setting with a default).
- **MAJOR** (2.0.0): anything that breaks an existing `config.json` or changes behaviour people rely on.

The version lives in one place: `src/starterstopper/__init__.py` (`__version__`). Changes are recorded in
`CHANGELOG.md` as they are made, under `## [Unreleased]`.

## Branches and remotes

- Day-to-day work happens on branches pushed to the private mirror (`origin`).
- `main` on GitHub (`github`) is the public, releasable line. Only merge to `main` what you would release.
- The pre-push hook (`git config core.hooksPath .githooks`) runs gitleaks and the private deny-list check on
  everything pushed to GitHub. CI runs the tests and gitleaks again on GitHub.

## Cutting a release

1. Move the `## [Unreleased]` entries into a new `## [X.Y.Z] - YYYY-MM-DD` section, and update the compare links at
   the bottom of `CHANGELOG.md`.
2. Set `__version__ = "X.Y.Z"` in `src/starterstopper/__init__.py`.
3. Check locally:

   ```powershell
   python -m pytest -q
   python tools/release_notes.py check vX.Y.Z
   ```

4. Commit (`Release vX.Y.Z`), merge to `main`, and tag:

   ```powershell
   git tag -a vX.Y.Z -m "StarterStopper X.Y.Z"
   git push github main vX.Y.Z
   git push origin main vX.Y.Z
   ```

5. The **release** workflow then:
   1. Runs gitleaks.
   2. Checks that the tag matches `__version__` and `CHANGELOG.md`.
   3. Runs the tests and the leak check.
   4. Builds `starterstopper-X.Y.Z.zip` with a SHA-256 checksum.
   5. Publishes a GitHub Release whose notes are the changelog section.

If the workflow fails, fix the problem, delete the tag (`git push github :refs/tags/vX.Y.Z`,
`git tag -d vX.Y.Z`), and tag again. Never move a tag once a release has been published; release a new patch
version instead.

The release zip leaves out development files (tests, workflows, hooks); see `.gitattributes`.
