# Contributing

Use Python 3.13 or newer and uv. Run `uv sync`, then `uv run poe check` before
opening a PR. Keep the 90% combined coverage gate and strict type checks intact.
Use Conventional Commit titles; squash merges drive release-please versioning.

Mutation testing is asynchronous on main, with a weekly full run and durable
[owner-assigned alerts](docs/mutation.md). It is not an eager PR, merge, or release
gate. Triage alerts within one working day and review open incidents weekly.

Keep unsupported contract behavior explicit. Add a failing regression before
changing translation, generated types, authentication semantics, network policy,
or file ownership. Never add credentials to fixtures or generated artifacts.
