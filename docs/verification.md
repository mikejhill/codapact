# First iteration verification

Verified on Windows with Python 3.13.15 and uv 0.11.32. Tests use synthetic
contracts, loopback HTTP servers, temporary output directories, and fixture-only
credentials. No real API credentials or private API calls were used.

- 118 unit, integration, property, security, generation, and fresh-install tests
  pass, with 92.51% combined line and branch coverage against a 90% gate.
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
not run because the repository has not been published. Mutation testing and
fuzzing beyond the included Hypothesis properties have not been run. No release
or package-registry publication has occurred.

Use `uv run poe check`, `uv build`, `uv run twine check --strict dist/*`, and
`uv run python scripts/verify_dist.py` to repeat the local gates. Workflow checks
use actionlint and zizmor; Markdown uses markdownlint-cli2.
