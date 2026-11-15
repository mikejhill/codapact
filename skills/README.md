# Agent skills

| Skill | Purpose |
| --- | --- |
| [create-coda-skill](create-coda-skill/SKILL.md) | Writes the thin skill that lets an agent use a coda's CLI |

Coda skills are thin by design: they run the CLI through `uvx` and point the agent at
`describe` and `doctor`, which every generated CLI carries (see
[Conventions every generated CLI follows](../docs/configuration.md)).

## Install

This repository is one plugin, `codapact`, in a marketplace of the same name. It ships a
root `plugin.json` in the [Agent Plugins](https://agent-plugins.org) format, which Codex and
GitHub Copilot read, and `.claude-plugin/plugin.json` for Claude Code.

| Agent | Add and install | Update |
| --- | --- | --- |
| Claude Code | `/plugin marketplace add mikejhill/codapact`, then `/plugin install codapact@codapact` | Off by default for this marketplace: turn on **Enable auto-update** under `/plugin` > Marketplaces, or run `claude plugin marketplace update codapact` then `/reload-plugins` |
| GitHub Copilot CLI | `copilot plugin marketplace add mikejhill/codapact`, then `copilot plugin install codapact@codapact` | `copilot plugin update codapact`, or set `autoUpdate: true` on the marketplace's `extraKnownMarketplaces` entry in user settings |
| Codex | `codex plugin marketplace add mikejhill/codapact`, then install `codapact` from the Plugins directory | `codex plugin marketplace upgrade codapact` |

No manifest pins a version, so each commit to `main` is a new version and updates pick it up.

Then ask "create the agent skill for codapact-ynab", or run the skill by name
(`/codapact:create-coda-skill` in Claude Code).

Any other agent that reads Agent Skills: copy `skills/create-coda-skill` into its skills
folder.
