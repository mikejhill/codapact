---
type: reference
status: active
authority: normative
criticality: normal
review:
  status: unreviewed
---

# Configuration

## `codapact.yaml`

```yaml
version: 1
source:
  type: openapi              # openapi | graphql
  path: openapi.yaml         # local path (relative to this file) or https URL
  documents: []              # companion documents, e.g. GraphQL operations
  options: {strict: true}
shape:                       # ordered transforms; the model is re-validated after each
  - group: {by: tag}
  - paginate: {operations: [list_pets], next: next_cursor, items: items}
surface: {flatten_body: true, trim_namespace: true}
cli:
  program: petstore
  auth_plugin: my_auth:provider   # optional; see Authentication
  auth: {sso: {client_id: petstore-cli, scopes: [read]}}
e2e:                         # optional; tests shipped with the generated CLI
  scenarios:
    - {name: list pets, run: [pets, list], expect: {not_empty: ["$"]}}
targets:
  - {name: python, output: out/python, options: {package: petstore}}
  - {name: python-cli, output: out/python}
  - {name: typescript, output: out/ts, options: {package: petstore}}
  - {plugin: [python, my_plugin.py], output: out/custom}
```

Targets that share an `output` directory share options. When `python-cli` shares the
`python` directory, the library's `pyproject.toml` gains the CLI script entry.

## Sources

| Source | Option | Default | Meaning |
| --- | --- | --- | --- |
| both | `strict` | `true` | Unsupported features are errors; when `false`, they degrade with a warning. For GraphQL, `false` also builds schemas that break spec rules (as GitHub's does) and lists the problems as warnings |
| `graphql` | `name` | `GraphQL API` | API name |
| `graphql` | `version` | empty | API version |
| `graphql` | `url` | none | Endpoint; becomes the default server |
| `graphql` | `auth` | none | `bearer`, `basic`, `{api_key: HEADER}`, or `{schemes: {NAME: SPEC}}` with any [auth spec](#auth-specs); required on every operation |
| `graphql` | `depth` | `1` | Object nesting synthesized in schema mode (0–8) |

`source.max_bytes` (next to `type` and `path`, not under `options`) caps each contract
document; the default is 64 MB. Remote contracts and generated clients honour
`HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY`. Proxy URLs must use `http://` without
credentials; HTTPS traffic is tunnelled with `CONNECT`.

GraphQL with `documents` generates one operation per document operation with exact result
types. Without documents, it generates one operation per root field. A `#` comment block
directly above a document operation becomes its description; variable defaults
(`$first: Int = 5`) become parameter defaults.

When the first shaping step is `select`, model errors in operations it removes are reported
as one warning instead of blocking generation.

## Shaping transforms

| Transform | Config |
| --- | --- |
| `select` | `include`, `exclude`: lists of globs or `{name, tag, kind, namespace, read_only}` selectors |
| `rename` | `operations`, `types`, `fields` (`Type.field`), `params` (`op.param`), `patterns` (`[{match: regex, replace: template}]` over operation names; explicit `operations` entries win); wire names are kept |
| `group` | `by: tag` or `by: path`, and/or `operations: {glob: dotted.namespace}` |
| `paginate` | `operations`, `style: cursor` (`param`, `next`), `offset` (`param`, `limit_param`), or `page` (`param`, default `page`; `start`, default `1`), and `items` (`$` when the result itself is the list). Operations matched only by a pattern that lack the param or items are skipped with a warning; operations named exactly are errors |
| `defaults` | `params: {"op_glob.param": value}`; the param becomes optional and the runtime sends `value` when it is omitted |
| `auth` | `schemes: {name: SPEC}` ([auth specs](#auth-specs)), `operations` (default all), `optional` (send credentials when set, call anonymously otherwise). For contracts that omit or under-declare `securitySchemes`; re-declaring an identical scheme is allowed |
| `prune` | none; removes unreachable types (also run by `select`) |

## Surface options

| Option | Default | Meaning |
| --- | --- | --- |
| `flatten_body` | `true` | Expose record body fields as individual method params |
| `flatten_wrapper` | `true` | A required body `{"account": {...}}` with one record field exposes that record's fields |
| `trim_namespace` | `true` | `pets.list_pets` becomes `pets.list`; a noun phrase ending in the namespace is dropped (`months.get_plan_months` → `months.list`); a plural/singular clash yields `update` and `update_many`; names that would still collide are kept |
| `conventional_names` | `true` | `get_<plural>` becomes `list_<plural>`; a trailing `by_id` is dropped (`get_pet_by_id` → `get_pet`) |

## Targets

| Target | Option | Default |
| --- | --- | --- |
| `python` | `package` | from API name |
| `python` | `distribution` | package with `-` |
| `python` | `version` | `0.1.0` |
| `typescript` | `package` | API name in kebab case |
| `typescript` | `version` | `0.1.0` |

## Generated CLI settings

Precedence: flag, then environment, then settings file.

| Setting | Flag | Environment | File key |
| --- | --- | --- | --- |
| Base URL | `--base-url` | `<PREFIX>_BASE_URL` | `base_url` |
| Timeout | `--timeout` | `<PREFIX>_TIMEOUT` | `timeout` |
| Private network | `--allow-private` | `<PREFIX>_ALLOW_PRIVATE` | `allow_private` |
| Response validation | `--response-validation strict\|warn\|off` | `<PREFIX>_RESPONSE_VALIDATION` | `response_validation` |
| Response size limit | none | `<PREFIX>_MAX_RESPONSE_BYTES` | `max_response_bytes` (default 100 MB) |
| Output format | `-o`/`--output json\|jsonl\|yaml\|table\|raw` | `<PREFIX>_OUTPUT` | `output` |
| Error format | `--error-format text\|json` | `<PREFIX>_ERROR_FORMAT` | `error_format` |
| Credentials | none | `<PREFIX>_<SCHEME>`; basic uses `_USERNAME`/`_PASSWORD`; full chain under [Authentication](#authentication) | `auth_plugin`; secrets are never read from the file |
| Option defaults | the option itself | none | `defaults: {plan_id: "…"}` (option names, `_` or `-`) |

The settings file is `--config PATH` or `$XDG_CONFIG_HOME/<program>/config.json`.
`<PREFIX>` is the program name in upper snake case. `<program> --help` lists the
credential variables for the contract's auth schemes.

Global options work before or after the command: `--base-url`, `--timeout`, `--config`,
`-o/--output`, `--query PATH` (`data.items[].name`, `data.items[0]`; with `--all` the
result is the item list, so `[].name`; a path that matches nothing fails with exit 2 and lists the keys that exist), `--dry-run`, `-v/--verbose`, `--error-format`,
`--allow-private`, and `--response-validation`. Command help lists them in one line;
`<program> --help` describes each.
`table` renders a list of objects, unwrapping single-list envelopes such as `{"data": {"items": [...]}}`.
`jsonl` prints one JSON value per line, unwrapping the same envelopes as `table`, and streams
`--all` results page by page. Output to a pipe or file is UTF-8 on every platform.

### Conventions every generated CLI follows

| Need | How |
| --- | --- |
| Help | `-h`/`--help` on every level; bare `<program>` or `<program> GROUP` prints help (exit `0`). Command help has a usage line with positionals and required options, examples, and a global-options reminder. |
| Version | `-V`/`--version`: `poke 0.1.0 (PokéAPI 2.10.0; codapact 0.4.0)` |
| Discovery for agents | `<program> describe [COMMAND…]`: JSON with every command's arguments (type, required, positional, choices, repeated), `read_only`/`destructive`/`paginated`, `requires_one_of` option groups, environment variables, exit codes. `describe --brief` keeps only names, summaries, and usage lines. Naming one command (or passing `--schemas`) adds the JSON Schema of its output and of each JSON-valued argument |
| Diagnosis | `<program> doctor [--offline]`: version, settings file, base URL and its source, credentials (set or missing, never printed), proxy, connectivity. Table on a terminal, JSON otherwise; exit `1` when a check fails |
| Completion | `<program> completion bash\|zsh\|fish\|powershell` prints a script that asks the program for candidates |
| Preview | `--dry-run` prints the HTTP request (method, URL, headers, body) without sending it; credentials appear as `$VARIABLE` placeholders (optional credentials that are not set stay off the request), and destructive commands skip confirmation |
| Tracing | `-v`/`--verbose` logs each request line and response status, size, and time to stderr, masking credential values |
| Mistakes | unknown commands, choices, and options get "did you mean" hints; leftovers are reported by the command they were given to; API failures name the command typed (`pets get failed with HTTP 404`) |
| Machine-readable errors | `--error-format json` prints one `{"error": {"type", "message", "exit_code", "status", "hint", "details"}}` object on stderr; contract warnings become `{"warning": …}` lines |

| Credentials | `<program> auth login\|logout\|status\|token`; see [Authentication](#authentication) |

`describe`, `doctor`, `completion`, and `auth` are skipped when the API has a top-level command
of the same name; `--version`, `--dry-run`, `--verbose`, and `--error-format` are reserved, so
API parameters with those names become `--arg-version` and so on.

`tests/integration/test_cli_conformance.py` checks these conventions for every example
contract (Petstore, Library, PokéAPI, YNAB, Countries), including a `--dry-run` of every
command built from its own `describe` entry.

Commands for HTTP `DELETE` operations, or operations named `delete_*`/`remove_*`/`destroy_*`/`purge_*`,
ask for confirmation on a terminal and require `--yes` otherwise.

Validation errors name the command-line option (`--amount: expected an integer`).

Required path parameters without a default are positional, in URL order
(`repos get OWNER REPO`); the `--owner` form still works.

Responses that break the contract (a `null` where the contract promises a string) print
a warning and the data by default (`warn`). `strict` fails with exit `1`; `off` skips
response checks. Generated libraries default to `strict` and raise
`ResponseValidationError`, whose `data` holds the response.

Exit codes: `0` success, `1` API error or contract break, `2` usage or validation, `3` auth
(missing credentials or HTTP 401/403), `4` transport, `130` interrupted. Root help and
`describe` list them.

## Authentication

Three layers, each usable without the next:

| Layer | What it is | Use it for |
| --- | --- | --- |
| Source | `AuthProvider.credential(scheme) -> Credential \| Authenticator \| None` | Where a secret comes from: environment, a command, the OS keyring, a vault |
| Built-in flow | `OAuth2(grant, store)` with `ClientCredentials`, `PasswordGrant`, `DeviceCode` (RFC 8628), `AuthorizationCodePKCE` (loopback redirect); refresh tokens; OIDC discovery | Standard OAuth 2.0 / OpenID Connect without code |
| Bespoke | `Authenticator.authenticate(request, scheme) -> request`, optional `refresh(scheme) -> bool` | Request signing (HMAC, AWS SigV4), token exchange, anything else |

An `Authenticator` runs last, after middleware, so a retried request is signed again. On
HTTP 401 the runtime calls `refresh()` and, if it returns true, sends the request once more.

### Auth specs

Used by the `auth` transform, the GraphQL `auth` option, and `cli.auth`:

| Spec | Meaning |
| --- | --- |
| `bearer`, `basic` | `Authorization` header |
| `{api_key: NAME, in: header\|query\|cookie}` | key sent under `NAME` |
| `oauth2` | bearer token; flows come from settings or `auth login` |
| `{oauth2: {client_credentials\|authorization_code\|device_code\|password: {token_url, authorization_url, device_authorization_url, refresh_url, scopes}}}` | OAuth 2.0 with known endpoints |
| `{openid: DISCOVERY_URL}` | OAuth 2.0 whose endpoints come from `.well-known/openid-configuration` |
| `custom` or `{custom: DESCRIPTION}` | needs an `Authenticator` (plugin or library code) |

Any spec map also takes `docs`. OpenAPI `oauth2` (including 3.2 `deviceAuthorization`) and
`openIdConnect` map to the same model; other schemes (`mutualTLS`, `http` with an unknown
scheme) become `custom` with an info diagnostic.

### CLI credential chain

The first source that has a credential wins, per scheme:

1. Plugin: `<PREFIX>_AUTH_PLUGIN`, settings file `auth_plugin`, or `cli.auth_plugin`,
   as `module:function`. The function takes an `AuthContext` (`api`, `program`,
   `env_prefix`, `environ`, `store`, `http`) and returns an `AuthProvider` or a
   `{scheme: Credential | Authenticator | str}` map.
2. Environment: `<PREFIX>_<SCHEME>` (basic: `_USERNAME`, `_PASSWORD`).
3. Command: `<PREFIX>_<SCHEME>_COMMAND`, run without a shell; stdout is the secret
   (`op read …`, `vault kv get …`, `gh auth token`).
4. OAuth client: `<PREFIX>_<SCHEME>_CLIENT_ID` + `_CLIENT_SECRET` (optional `_SCOPES`)
   runs the client-credentials grant and caches the token.
5. Saved login from `<program> auth login`.

`cli.auth: {SCHEME: {client_id, scopes, flow, audience}}` bakes public OAuth settings into
the CLI (no secrets). `auth login` picks a flow in this order: `--flow`, then client
credentials when a secret is set, then device code, then authorization code with PKCE.
`--with-token` reads a static token from stdin; `--username` saves basic credentials.
`auth status` shows each scheme's source; `auth token` prints what a command would send;
`doctor` reports the same, never printing secrets.

Saved credentials and tokens go to the OS keyring when one is usable, otherwise to a
`0600` JSON file at `$XDG_CONFIG_HOME/<program>/credentials.json`.
`<PREFIX>_CREDENTIAL_STORE=keyring|file|memory` forces one.

Libraries take the same objects directly:

```python
from codapact.runtime import ClientCredentials, OAuth2, StaticAuth

client = Client(
    auth=StaticAuth(
        {
            "token": "…",  # bearer / api key
            "sso": OAuth2(ClientCredentials(token_url, client_id, secret)),
            "signed": HmacSigner(key_id, secret),  # your Authenticator
        }
    )
)
```

The TypeScript client accepts a string, `{username, password}`, a token function, an
`Authenticator` object, or `clientCredentials({tokenUrl, clientId, clientSecret})`.

## Generated bundle tests

`python-cli` writes `tests/test_e2e.py` and `tests/e2e.json` into the bundle
(`options: {tests: false}` turns this off). Run them with `uv run pytest` in the bundle.

| Tier | Runs | Checks |
| --- | --- | --- |
| Offline | always | `--help` of every command; a `--dry-run` of every command with sampled arguments: method, path, and where each credential goes |
| Live | `<PREFIX>_E2E_LIVE=1` (or `CODAPACT_LIVE=1`) | `e2e.scenarios`, then a smoke call of each read-only command that needs no arguments |

A live case whose credentials are not configured is skipped;
`<PREFIX>_E2E_REQUIRE_AUTH=1` makes it fail instead.

| `e2e` key | Meaning |
| --- | --- |
| `scenarios` | list of `{name, run, auth, expect, env, response_validation, stdin}`; `run` is the argument list after the program name |
| `scenarios[].auth` | `any` (default), `required` (skip without credentials), `none` (run with every credential removed) |
| `scenarios[].expect` | `exit` (default `0`), `contains: {PATH: value}` (subset match), `not_empty: [PATH]`, `at_least: {PATH: n}`, `length_at_least: {PATH: n}`, `stderr_contains`; `PATH` uses the `--query` syntax, `$` is the whole result |
| `smoke` | `true` (default), `false`, or `{limit: 25, exclude: [glob]}` over command paths |
| `response_validation` | mode for every live case (default `strict`) |

## Generated names by target

| Target | Method params | Record fields |
| --- | --- | --- |
| Python | `snake_case` (keywords get a trailing `_`) | model names; the runtime codec maps them to wire names |
| TypeScript | `camelCase` | wire names; the bundled runtime sends values as typed, without a codec |

So `rename.fields` renames a Python `TypedDict` key but not a TypeScript type key. Method
parameters follow each language's convention in both targets.

## Formats

`format` values (`date`, `date-time`, `uuid`, …) are annotations, as in JSON Schema 2020-12:
the client does not reject values that do not match them, because real APIs accept extra
forms (YNAB's `month` path param is `format: date` but also takes `current`). CLIs show
`YYYY-MM-DD` / `DATETIME` as the option placeholder.
