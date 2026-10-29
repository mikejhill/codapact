# First iteration verification

Verified on Windows with Python 3.13.15 and uv 0.11.32. Tests use synthetic
contracts, loopback HTTP servers, temporary output directories, and fixture-only
credentials. No real API credentials or private API calls were used.

- 216 unit, integration, property, security, generation, and fresh-install tests
  pass on Windows, with 94% combined line and branch coverage against a 90% gate.
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

Hosted CI passed across Ubuntu and Windows on Python 3.13 and 3.14 for the initial
public checkpoint. The [CI workflow](https://github.com/mikejhill/codapact/actions/workflows/ci.yml)
shows subsequent checks. A separate isolated Linux baseline passed. A bounded
WSL mutation experiment killed all 17 selected operation-policy mutants. Its
first run found a missing assertion for useful denial diagnostics; the regression
now requires the denied operation's identity in the error. The full generated
inventory contained 2,763 mutants, so this is partial scope and cannot establish
a project score. The first complete hosted main run measured 65.94% (1,822
killed, 941 survived, 2,763 total), below the unchanged 90% floor. Its durable
[regression issue](https://github.com/mikejhill/codapact/issues/5) is assigned for
triage. Named mutant details and source snapshots are retained as workflow
artifacts. [Asynchronous full runs and alerts](mutation.md) provide continuing
evidence without blocking release.
Fuzzing beyond the included Hypothesis properties has not been run. Package
publication is verified separately through the release workflow and official
PyPI metadata.

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
