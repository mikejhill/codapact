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
  `options: {package: codapact_<api>, distribution: codapact-<api>}`.
- `gen/`: generated, never edited by hand; CI regenerates and fails on a diff.
- Root `pyproject.toml`: hand-written PyPI metadata and version (owned by release-please),
  building from `gen/python/src`.
- `skills/codapact-<api>/SKILL.md`: the agent skill, written with
  [create-coda-skill](../extras/agent-plugin/skills/create-coda-skill/SKILL.md).

Run a coda through `uvx`; do not document `uv tool install` as the way to run it.
