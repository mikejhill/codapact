---
type: overview
status: active
authority: informative
criticality: low
review:
  status: unreviewed
---

# Development

Run contributor commands from the repository root. uv manages Python, dependencies,
and the environment; Poe exposes the repeatable checks.

```console
uv sync --locked
uv run poe fix
uv run poe check
uv build
uv run twine check --strict dist/*
```

Tests isolate home/XDG directories and use synthetic credentials and loopback
servers. Keep the strict ty settings and 90% coverage floor. Mutation testing
runs asynchronously; it remains separate from this gate.

- [Verification](verification.md): historical evidence, current scope, and limits.
- [Documentation convention](documentation.md): structure, metadata, and authority.
- [Mutation review artifact](mutation-review-ab121cb.json): historical classifications;
  source identities predate the package refactor and must not be reused as exclusions.

See [contributing](../../CONTRIBUTING.md) and [architecture](../architecture/README.md)
before changing extension interfaces.
