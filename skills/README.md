# Agent skills

| Skill | Purpose |
| --- | --- |
| [create-coda-skill](create-coda-skill/SKILL.md) | Writes the thin skill that lets an agent use a coda's CLI |

Coda skills are thin by design: they install the CLI and point the agent at `describe` and
`doctor`, which every generated CLI carries (see
[Conventions every generated CLI follows](../docs/configuration.md)).

## Install

Claude Code, as a plugin from this repository:

```text
/plugin marketplace add mikejhill/codapact
/plugin install codapact@codapact
```

Then ask "create the agent skill for codapact-ynab", or run `/codapact:create-coda-skill`.

Any agent that reads Agent Skills (Claude Code, GitHub Copilot, Codex): copy
`skills/create-coda-skill` into its skills folder, such as `~/.claude/skills/`.
