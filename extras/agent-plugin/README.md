# Codapact agent plugin

| Skill | Purpose |
| --- | --- |
| [create-coda-skill](skills/create-coda-skill/SKILL.md) | Writes the thin skill that lets an agent use a coda's CLI |

Coda skills are thin by design: they run the CLI through `uvx` and point the agent at
`describe` and `doctor`, which every generated CLI carries (see
[Conventions every generated CLI follows](../../docs/configuration.md)).

## Layout

Everything lives in this folder except the marketplace catalog,
`.claude-plugin/marketplace.json` at the repository root. Claude Code, GitHub Copilot, and
Codex only find a catalog there when you add the repository as `owner/repo`.

| File | Read by |
| --- | --- |
| `plugin.json` | Codex and GitHub Copilot ([Agent Plugins](https://agent-plugins.org) 1.0) |
| `.claude-plugin/plugin.json` | Claude Code |
| `skills/` | All of them |

## Install

| Agent | Add and install | Update |
| --- | --- | --- |
| Claude Code | `/plugin marketplace add mikejhill/codapact`, then `/plugin install codapact@codapact` | Off by default for this marketplace: turn on **Enable auto-update** under `/plugin` > Marketplaces, or run `claude plugin marketplace update codapact` then `/reload-plugins` |
| GitHub Copilot CLI | `copilot plugin marketplace add mikejhill/codapact`, then `copilot plugin install codapact@codapact` | `copilot plugin update codapact` |
| Codex | `codex plugin marketplace add mikejhill/codapact`, then `codex plugin add codapact@codapact` | `codex plugin marketplace upgrade codapact` |

No manifest pins a version, so each commit to `main` is a new version for Claude Code.

Then ask "create the agent skill for codapact-ynab", or run the skill by name
(`/codapact:create-coda-skill` in Claude Code).

Any other agent that reads Agent Skills: copy `skills/create-coda-skill` into its skills
folder.
