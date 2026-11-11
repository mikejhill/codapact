---
type: reference
status: active
authority: informative
criticality: normal
review:
  status: unreviewed
---

# Contributing

Use Python 3.13 or newer and uv. Run `uv sync`, then `uv run poe check` before
opening a PR. Keep the 95% branch-coverage gate, 100% coverage of changed lines, and
strict type checks intact. See [testing](docs/testing.md) for the suites and gates.
Use Conventional Commit titles; squash merges drive release-please versioning.

Respect the layer map in [architecture](docs/architecture.md). `tests/unit/test_architecture.py`
fails the build when a module imports a layer it must not depend on.

- New contract format: add a `Source` under `sources/`; it may only produce the semantic model.
- New language: add a `Target` under `targets/` (or ship an external plugin); targets must
  not import each other.
- New shaping step: add a `Transform`; the engine re-validates the model after every step.
- Changed model shape: bump `MODEL_VERSION` and add a migration in `model/migrations.py`.

Keep unsupported contract behavior explicit: strict mode errors, lenient mode warns.
Add a failing regression before changing translation, generated code, authentication,
network policy, or file ownership. Never add credentials to fixtures or generated artifacts.
