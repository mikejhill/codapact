---
type: reference
status: active
authority: normative
criticality: normal
review:
  status: reviewed
---

# Publishing codas

A coda is a Codapact-generated implementation of one API, published as its own repository
and package (e.g. the YNAB coda, [codapact-ynab](https://github.com/mikejhill/codapact-ynab)).
This page is the policy for codas the Codapact project publishes itself. Codapact does not
impose it on anyone else's generated code: its own defaults derive names from the API.

## Naming

Every self-published coda uses the prefix `codapact-<api>` in every name:

| Name | Value | YNAB example |
| --- | --- | --- |
| GitHub repository | `codapact-<api>` | `mikejhill/codapact-ynab` |
| PyPI distribution | `codapact-<api>` | `codapact-ynab` |
| Python package | `codapact_<api>` | `codapact_ynab` |
| Default command | the distribution name | `codapact-ynab` |

`<api>` is the product's short name in lower kebab case (`ynab`, `github`, `pokeapi`), not
the contract title.

Never `coda-<api>`, `<api>-coda`, `<api>-cli`, or a bare `<api>`.

Why:

- **Plugin-family precedent.** `pytest-*`, `mkdocs-*`, and `sphinxcontrib-*` put the
  family first; users already read a prefix as "built on that tool".
- **No implied vendor affiliation.** `ynab-cli` reads as YNAB's own tool;
  `codapact-ynab` says who made it.
- **Grouping.** All codas sort together in PyPI and GitHub search and in shell tab
  completion (`codapact-<TAB>`).
- **No collision.** `coda-` already means Coda.io and its packages.

Rules that follow from it:

- The command defaults to the distribution name (do not set `cli.program`), so
  `uvx codapact-<api>` runs it with no `--from`.
- Set `cli.env_prefix` to the short API name (e.g. `YNAB`) so environment variables stay
  short (`YNAB_BEARER`, not `CODAPACT_YNAB_BEARER`).
- A short alias for the command is allowed on top, as an extra `[project.scripts]` entry.
  Never rename the distribution, repository, or default command for brevity.
- Renaming an existing coda to this scheme: list the old command names in
  `cli.previous_programs` so saved logins and settings carry over
  (see [Program name](configuration.md#program-name)).

## Repository layout

Follow [codapact-ynab](https://github.com/mikejhill/codapact-ynab):

- `openapi.yaml` (or the GraphQL schema): the pinned contract; `source.upstream` names the
  remote copy that `codapact sync --check` watches.
- `codapact.yaml`: targets `python` and `python-cli` into `gen/python`, with
  `options: {package: codapact_<api>, distribution: codapact-<api>}`, and `python-readme`
  into `.` (see [README](#readme)).
- `README.md`: generated; never edited by hand.
- `gen/`: generated, never edited by hand; CI regenerates and fails on a diff.
- Root `pyproject.toml`: hand-written PyPI metadata and version (owned by release-please),
  building from `gen/python/src`.
- `cli.skill` in `codapact.yaml`: the agent skill, shipped in the package and installed
  with `codapact-<api> skill install --dir DIR` (see
  [Agent skill](configuration.md#agent-skill) and
  [create-coda-skill](../extras/agent-plugin/skills/create-coda-skill/SKILL.md)).

- `.github/workflows/release.yml`: release-please and trusted publishing, with the
  standard [release token](#release-token).

Run a coda through `uvx`; do not document `uv tool install` as the way to run it.

## Releases

Every repository the project publishes (Codapact, adflux, and each coda) releases the same
way, and no release needs anyone's approval:

1. Conventional Commit squash merges land on `main`.
2. `release.yml` runs release-please, which opens or updates the release pull request.
3. The release pull request's checks run and pass; it is merged like any other pull request.
4. The merge tags the release, and `release.yml` runs CI, builds, and publishes to PyPI
   through trusted publishing (environment `pypi`, no protection rules).

### Release token

The `release-please` step in `release.yml` takes its token from the release GitHub App when
the repository has one, and from `GITHUB_TOKEN` otherwise:

```yaml
- id: app-token
  if: vars.RELEASE_APP_CLIENT_ID != ''
  uses: actions/create-github-app-token@<sha> # v3.x
  with:
    client-id: ${{ vars.RELEASE_APP_CLIENT_ID }}
    private-key: ${{ secrets.RELEASE_APP_PRIVATE_KEY }}
    permission-contents: write
    permission-pull-requests: write
    permission-issues: write
- id: release
  uses: googleapis/release-please-action@<sha> # v5.x
  with:
    token: ${{ steps.app-token.outputs.token || secrets.GITHUB_TOKEN }}
- name: Start CI for release pull request
  if: steps.release.outputs.prs_created == 'true' && steps.app-token.outcome != 'success'
  run: gh workflow run ci.yml --ref "$branch"   # and codeql.yml where the repo has one
```

- **With the App** (preferred): release-please pushes as the App, so the release pull
  request's own `pull_request` checks start normally.
- **Without it**: GitHub holds `pull_request` runs for pushes made with `GITHUB_TOKEN` as
  "approval required". `release.yml` dispatches the same workflows on the release branch
  instead; those runs report the required checks for the branch head, so the pull request
  still merges without approval. The held runs show on the pull request but block nothing.

The job needs `actions: write` for the dispatch fallback. Copy the block from an existing
repository's `release.yml` so the pinned action SHAs match.

### Release App setup

One GitHub App serves every repository:

- Permissions: Contents, Pull requests, and Issues read and write; no webhook.
- Installed on the owner account for each repository that releases.
- Each repository has variable `RELEASE_APP_CLIENT_ID` (the App's client ID) and secret
  `RELEASE_APP_PRIVATE_KEY` (a private key of the App).

A repository without them uses the fallback above; nothing else changes.

## README

Every coda's README is generated in the [common format](configuration.md#readme), so all
codas read the same. Set the `readme` section of `codapact.yaml`:

```yaml
readme:
  name: YNAB API                         # product name of the API
  docs: https://api.ynab.com/            # its public documentation
  repository: mikejhill/codapact-ynab    # GitHub: badges and links
  examples: [plans list, accounts list -o table]
  sections:                              # anything coda-specific, e.g. upgrade notes
    - {title: Notes, body: "..."}
```

- **Source and version.** With `docs` and `source.upstream` set, the README links the API,
  the pinned contract and its upstream, and records the contract version and SHA-256 and
  the Codapact version it was generated with. CI regenerates and fails on a diff, so this
  always matches the code.
- **Badges.** A coda on GitHub shows all of them: CI, PyPI version, Python versions,
  license, API version, and Codapact version.
- **No published API.** For a service without a public API, leave out `docs` and
  `source.upstream` and set `about` to one sentence saying what the coda is, e.g.
  "`codapact-acme` is a command-line tool for making calls to Acme." The README then has no
  API source section and no API badge. Say nothing about how the contract was written.
- **Not on PyPI yet.** Without `repository` (or with `pypi: false`), the README runs the coda
  with `uvx --from ./gen/python codapact-<api>`.
- **Agent skill.** With `cli.skill` set, the README gets an "Agent skill" section on its own.
