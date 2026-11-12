---
type: reference
status: active
authority: normative
criticality: normal
review:
  status: unreviewed
---

# Testing

codapact turns contracts into code other people run, so a bug ships to every
generated client. The suites below each catch a different class of bug; the gates
make sure they keep running.

## Suites

| Suite | Directory / marker | What it proves | Runs |
| --- | --- | --- | --- |
| Unit | `tests/unit`, `unit` | Each layer against hand-built inputs; every guard rail rejects bad input precisely | every PR, every OS/Python |
| Integration | `tests/integration`, `integration` | Layers composed in-process: generated code compiles, imports, type-checks, runs | every PR |
| Property | inside `integration` (Hypothesis) | Contract-level invariants on fuzzed inputs (below) | every PR (300 examples), nightly (3000) |
| Wire parity | `integration/test_wire_parity.py` | Python and TypeScript clients send byte-identical requests | every PR |
| Corpus | `integration/test_corpus.py` | Real contracts (PokeAPI, YNAB, GitHub slice) generate in strict mode with no warnings | every PR |
| CLI conformance | `integration/test_cli_conformance.py` | Every example CLI follows the CLI conventions; every command dry-runs from its `describe` entry | every PR |
| End to end | `tests/e2e`, `e2e` | `python -m codapact` and generated CLIs as subprocesses over loopback HTTP; clean-venv install | every PR |
| Prism | `e2e/test_public_apis.py` + `CODAPACT_PRISM_URL` | Every request validated by a Prism mock of the pinned PokeAPI spec | every PR |
| Auth | `e2e/test_auth_e2e.py` | Generated CLIs, bundle suites, and TypeScript clients authenticate against AuthLab (`tests/authlab.py`), a loopback server that answers 401 unless the scheme is satisfied: API keys (header, query, cookie), basic, bearer, OAuth 2.0 client credentials, device code, authorization code + PKCE, refresh, and an HMAC-signing plugin | every PR |
| Live | marker `live`, `CODAPACT_LIVE=1` | PokeAPI and Countries GraphQL respond as the generated CLI expects; the GitHub corpus bundle runs its own live suite with `GITHUB_TOKEN` (authenticated quota, invalid token rejected) | nightly |
| Performance | `tests/perf`, `perf` | Translation, generation, and decoding scale linearly; throughput floors | every PR |
| Mutation | `mutmut`, 8 parallel shards | Tests notice behavior changes in `src/codapact` | after merge to main, weekly |

Suite markers come from the directory (`tests/conftest.py`); a new file needs no marker.

## Property tests

`tests/strategies.py` draws OpenAPI 3.1 contracts: primitives with constraints, enums
(nullable included), objects with colliding property names and keywords, arrays, maps,
`$ref`, `allOf` with property and `required`-only members, `oneOf`/`anyOf`, and
query/header parameters. The oracle is independent of codapact:
[`hypothesis-jsonschema`](https://github.com/python-jsonschema/hypothesis-jsonschema)
draws instances valid against the *original* schema.

- Any valid response decodes, and re-encodes to the same wire value.
- Any valid request body is sent unchanged.
- Any valid parameter set is accepted and sent.
- Any contract codapact accepts yields Python that compiles, imports, and renders help
  for every command, and TypeScript that passes `tsc --strict`.

These found the YNAB null-enum bug independently, plus property-name collisions
(`fooBar` vs `foo_bar`, `+1` vs `-1`), header and cookie injection, and JSON-encoded
header arrays. When one fails, Hypothesis prints the minimal contract: add it as a
unit regression next to the fix.

## Gates

| Gate | Threshold | Where |
| --- | --- | --- |
| Branch coverage, whole package | 95% | `poe test`, every CI cell |
| Coverage of changed lines | 100% | `coverage` job (`diff-cover` vs. base branch) |
| TypeScript tests run | must not skip | `CODAPACT_REQUIRE_NODE=1` in CI |
| Scaling ratio (4x input) | < 8x time | `perf` job |
| Mutation score, per shard | 70% (baseline ~74%; raise, never lower) | `mutation` workflow |

Subprocesses are measured too (`[tool.coverage.run] patch = ["subprocess"]`), so e2e
runs count. Code that genuinely cannot run in tests takes `# pragma: no cover` with a
reason; that is reviewed like any other change.

## Running locally

```sh
uv run poe check             # lint, types, unit + integration + e2e with coverage
uv run poe test-unit         # or test-integration, test-e2e, test-perf
uv run poe test-deep         # 3000 property examples, 300 codegen fuzz cases
uv run poe test-live         # real public APIs (network)
uv sync --group mutation && uv run poe mutation && uv run poe mutation-score
```

TypeScript tests need Node 22.6+ and `tsc` on `PATH` (`npm i -g typescript`); without
them they skip locally. For the Prism tier:
`npx @stoplight/prism-cli@5.16.0 mock --errors -p 4010 tests/fixtures/corpus/pokeapi/openapi.yml`
then `CODAPACT_PRISM_URL=http://127.0.0.1:4010 uv run pytest tests/e2e/test_public_apis.py`.

## Adding tests

- Bug fix: a failing regression in the narrowest suite first (CONTRIBUTING requires it).
- New construct in a source: extend `tests/strategies.py` so the property tests cover it.
- New target: add it to `test_generated_code_properties.py` and to the wire-parity calls.
- New real-world contract: drop it in `tests/fixtures/corpus/` and list it in
  `test_corpus.py` with its license in the corpus README.
