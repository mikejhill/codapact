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

## Publisher and bot setup

The public source destination is `mikejhill/codapact`. The approved PyPI pending
publisher binds project `codapact` to that repository, workflow `release.yml`,
and environment `pypi`. `RELEASES_ENABLED=true` enables the version workflow.

GitHub currently forbids its Actions bot from creating PRs. Release-please can
still prepare the release branch; an authorized maintainer can open that branch
as a PR and add `autorelease: pending`. After checks pass, squash-merge it using
the generated Conventional Commit release title. The next main workflow creates
the tag/release and publishes through OIDC. No token or expanded bot permission
is required for this manual PR step. Confirm only intended version/changelog/
lockfile changes are included and that `CI passed` succeeds.

Mutation analysis remains asynchronous and is not part of `CI passed` or the
release dependency chain. Its durable alerts are described in [mutation.md](mutation.md).

The workflow dispatches CI on release PR branches because the default GitHub
token suppresses follow-on workflow events. An optional `RELEASE_PLEASE_TOKEN`
can also trigger normal PR checks. Review the repository's bot-run policy before
changing bot permissions. No PR-approving review, API token, or repository
security setting is needed for the manual PR step.

Before publishing, verify the finalized package name, metadata, release
PR checks, clean-environment installation, and publisher account. The initial
baseline is 0.1.0. `uv build`, `uv run twine check --strict dist/*`, and
`uv run python scripts/verify_dist.py` verify local artifacts.
