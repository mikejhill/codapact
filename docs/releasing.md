# Releases

The project follows VerdiClip's current standard: uv builds, Conventional
Commits, SemVer, release-please release PRs, changelogs, `vX.Y.Z` tags,
release-commit CI, one wheel/sdist build, version verification, provenance
attestations, and PyPI trusted publishing. Sources inspected were VerdiClip's
`docs/releasing.md`, `release-please-config.json`, `pyproject.toml`, and
`.github/workflows/release.yml` on main on 2026-10-29.

Squash-merge PRs using Conventional Commit titles. `fix:` and `perf:` bump patch;
`feat:` bumps minor. Breaking changes bump minor below 1.0, major afterwards.
The release PR updates project/runtime/lockfile versions and `CHANGELOG.md`.
The runtime version also determines generated dependencies and manifests.

## Pending setup

Nothing is published. `RELEASES_ENABLED` is unset, so the release workflow is
disabled until the repository destination/visibility and PyPI publisher are
established. Create the approved repository, configure the trusted publisher for workflow
`release.yml` and environment `pypi`, and require the `CI passed` check.

The workflow dispatches CI on release PR branches because the default GitHub
token suppresses follow-on workflow events. An optional `RELEASE_PLEASE_TOKEN`
can also trigger normal PR checks. Review the repository's bot-run policy before
enabling releases. No PR-approving review,
credential provisioning, or repository security setting was changed here.

Before enabling publishing, verify the finalized package name, metadata, release
PR checks, clean-environment installation, and publisher account. The initial
baseline is 0.1.0. `uv build`, `uv run twine check --strict dist/*`, and
`uv run python scripts/verify_dist.py` verify local artifacts.
