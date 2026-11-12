# Real-world contract corpus

Public API contracts that codapact must translate in **strict** mode and turn
into code that compiles, imports, and type-checks. They are pinned copies so the
tests are hermetic; refresh them deliberately.

| Contract | Source (pinned) | License |
| --- | --- | --- |
| `pokeapi/openapi.yml` | `PokeAPI/pokeapi@5cbeb3f3eb55a9b86da52c4869336f9d61fdd6d7` `openapi.yml` (v2.10.0) | BSD-3-Clause |
| `countries/schema.graphql` | hand-written subset of countries.trevorblades.com (used offline by CLI conformance) | written for this repo |
| `ynab/openapi.yaml` | `ynab/ynab-sdk-js@main` `open_api_spec.yaml` (v1.88.0, fetched 2026-11-11) | Apache-2.0 |

`tests/integration/test_corpus.py` lists the expectations (operation counts,
strictness). Add a contract by dropping it here and adding a row there.
