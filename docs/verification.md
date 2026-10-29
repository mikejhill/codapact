# First iteration verification

Verified on Windows with Python 3.13.15 and uv 0.11.32. Tests use synthetic
contracts, loopback HTTP servers, temporary output directories, and fixture-only
credentials. No real API credentials or private API calls were used.

- 165 unit, integration, property, security, generation, and fresh-install tests
  pass on Windows, with 92.84% combined line and branch coverage against a 90% gate.
- The distribution test downloads a contract from the local fixture server,
  generates a package, builds both runtime and generated wheels, installs them
  into a fresh environment, and calls the server through the installed typed
  library and installed command entry point.
- Local JSON/YAML loading, internal references, malformed input, oversized input,
  unsafe YAML, expansion limits, unsafe names, and path traversal are exercised.
- Authentication covers bearer, basic, header API key, security inheritance,
  OR alternatives, AND requirements, anonymous overrides, missing credentials,
  header conflicts, and newline rejection.
- Custom authentication, authorization, transport, contract translator, and
  generator implementations are exercised. Plugin API/capability mismatches fail.
- HTTP/API/schema errors are tested with assertions that secrets are absent from
  diagnostics. DNS pinning and TLS hostname selection are verified with transport
  doubles. Real public-network TLS was not exercised.
- Regeneration is deterministic, preserves unrelated files, removes obsolete
  owned files, and refuses edited or unowned output.
- A 100-operation contract translates and generates compilable source within a
  ten-second regression budget. This is a smoke budget, not a production benchmark.
- Runtime and generated-package wheels are inspected before fresh installation.
  Source distributions include tests and documentation and exclude temporary
  environments and caches. Strict package metadata and version checks pass.

CI is configured for Ubuntu and Windows on Python 3.13 and 3.14. Hosted CI has
not yet run for the publication checkpoint. A separate isolated Linux baseline
passed. A bounded WSL mutation experiment completed 17 operation-policy mutants:
16 were killed and one survived for inspection. The full generated inventory
contained 2,734 mutants, so this is partial scope and cannot establish a project
score. [Asynchronous full runs and alerts](mutation.md) provide continuing evidence.
Fuzzing beyond the included Hypothesis properties has not been run. No package
release or registry publication has occurred.

The second adversarial pass first reproduced 15 failures, then added regressions
for canonical JSON Pointers, strict configuration/plugin versions, stable
generated type names, ambiguous URLs, integral-number mapping, nonfinite values,
and non-string object keys. Empty authentication hooks no longer satisfy a
required security scheme. Mutation alert tests cover pagination, deduplication,
recurrence, malformed evidence, stale/incomparable results, weekly reminders,
namespace isolation, and recovery. Branding was rasterized and visually checked
without desktop input.

Use `uv run poe check`, `uv build`, `uv run twine check --strict dist/*`, and
`uv run python scripts/verify_dist.py` to repeat the local gates. Workflow checks
use actionlint and zizmor; Markdown uses markdownlint-cli2.
