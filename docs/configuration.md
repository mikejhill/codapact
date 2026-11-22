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
  upstream: https://api.example.com/openapi.yaml   # optional; remote copy `codapact sync` tracks
  documents: []              # companion documents, e.g. GraphQL operations
  options: {strict: true}
shape:                       # ordered transforms; the model is re-validated after each
  - group: {by: tag}
  - paginate: {operations: [list_pets], next: next_cursor, items: items}
surface: {flatten_body: true, trim_namespace: true}
cli:
  program: petstore           # optional; default: the Python distribution that ships the CLI
  env_prefix: PETSTORE        # optional; environment variable prefix (default: program)
  previous_programs: [pets]   # optional; earlier names whose logins and settings carry over
  auth_plugin: my_auth:provider   # optional; see Authentication
  auth: {sso: {client_id: petstore-cli, scopes: [read]}}
readme:                      # optional; README of the Python package (see README)
  name: Petstore API
  docs: https://petstore.example.com/docs
  repository: acme/codapact-petstore
e2e:                         # optional; tests shipped with the generated CLI
  scenarios:
    - {name: list pets, run: [pets, list], expect: {not_empty: ["$"]}}
targets:
  - {name: python, output: out/python, options: {package: petstore}}
  - {name: python-cli, output: out/python}
  - {name: python-readme, output: .}   # the same README at the repository root
  - {name: typescript, output: out/ts, options: {package: petstore}}
  - {plugin: [python, my_plugin.py], output: out/custom}
```

Each output directory gets a `.codapact-manifest.json`: the SHA-256 of every generated file
(used to refuse overwriting edits), the `generator` (`codapact/<version>`), `source` (type,
location, upstream, and SHA-256 of the contract and each document), `config_sha256` of the
project file, and `generated_at` (UTC). The timestamp only changes when anything else in the
manifest changes, so regenerating identical output leaves every file untouched.

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
credentials; HTTPS traffic is tunnelled with `CONNECT`. Certificates are verified against
the operating system's trust store (Windows certificate store, macOS keychain, the system
bundle on Linux), so TLS-inspecting corporate proxies whose root certificate is installed
there work without extra settings; `SSL_CERT_FILE` names a PEM bundle to use instead.
Redirects are never followed: a generated client reports a 3xx as an error that names
its `Location`.

YAML contracts are read with YAML 1.2 rules, as OpenAPI requires: `on`, `off`, `yes`,
`no`, `=`, `12:30` and dates stay strings, exactly as in the equivalent JSON.

Request bodies are sent as JSON when the contract offers it, else as
`multipart/form-data` or `application/x-www-form-urlencoded` (object fields become form
fields; binary fields become file parts; a bare binary body, or a list of files, is sent as
parts named `file`),
else as text or raw bytes. `*/*` sends raw bytes for a binary or schema-less body and JSON
otherwise. Binary values are `bytes`, a `str`, or a path (`os.PathLike`) in libraries, and
`TEXT`, `@FILE` or `@-` (stdin) on the command line; a file is sent under its own name.

### Pinned contracts and `codapact sync`

Generating from a remote `path` always uses the remote's current version. To pin a local copy
instead and still notice when the provider changes it, set `source.path` to the local file
and `source.upstream` to its URL:

| Command | Effect | Exit code |
| --- | --- | --- |
| `codapact sync` | Replaces the local file with the upstream bytes when they differ | 0, or 1 on error |
| `codapact sync --check` | Reports drift without writing (for CI) | 0 current, 3 out of date, 1 error |
| `--from URL` | Overrides `source.upstream` for one run | |

A copy that differs only in line endings is current. A copy that parses to the same JSON or YAML
as upstream (formatting only) passes `--check` and is rewritten by `codapact sync`. Drift
reports `info.version` old and new plus changed line counts, e.g.
`out of date (version 1.88.0 -> 1.89.0, +120 -30 lines): openapi.yaml vs https://...`.
`source.documents` are not synced.

`--check` also reads each target's `.codapact-manifest.json` and reports output generated from an
older local contract (`generated output is behind openapi.yaml: gen/python`), also exit 3. A CI
step that warns but does not fail:

```yaml
- run: codapact sync --check || { [ $? -eq 3 ] && echo "::warning::contract is out of date"; }
```

GraphQL with `documents` generates one operation per document operation with exact result
types. Without documents, it generates one operation per root field. A `#` comment block
directly above a document operation becomes its description; variable defaults
(`$first: Int = 5`) become parameter defaults.

When the first shaping step is `select`, it also filters before translation: operations it
excludes are never read, so problems in them (unsupported media types, odd enums, invalid
parameters) cannot block generation, and problems in schemas only those operations use are
reported as one warning. Model errors in operations it removes are likewise one warning.
This is the way to generate part of a very large contract in strict mode.

## Custom protocols

APIs that are not plain REST or GraphQL (GWT-RPC, JSON-RPC, XML-RPC, positional form
posts, vendor RPC formats) are still described with OpenAPI, so types, parameters,
shaping, the library surface, and the CLI work as usual. An `x-codapact-protocol`
extension names the protocol that really carries each operation:

```yaml
openapi: 3.1.0
x-codapact-protocol: {name: gwt-rpc, service: com.example.AppService}  # default for every operation
paths:
  /days/{date}:                       # path and method only shape names and the CLI
    get:
      operationId: getDay
      x-codapact-protocol: {method: getDayInfo, signature: [java.lang.String]}
  /api/v3/targets:
    get:
      operationId: getTargets
      x-codapact-protocol: false      # an ordinary HTTP operation
```

| Level | Meaning |
| --- | --- |
| document, path item, operation | `false` (no custom protocol from here down), a protocol name, or a mapping; nearer levels override keys of outer ones |
| mapping | `name` is the protocol; every other key is a JSON option the handler reads from `binding.options` |

Such an operation gets a `CustomBinding(protocol, options)` next to its HTTP binding. The
custom binding is the one executed; the HTTP binding only names parameters and shapes
commands, and is never sent. Calling an operation whose protocol has no handler fails with
an error naming the protocol, rather than sending the placeholder request.

A handler is a factory `(Exchange) -> handler` whose `call(operation, binding, arguments)`
returns the decoded result. `arguments` are the validated, wire-encoded parameters keyed by
parameter name. `codapact.runtime.send_authorized(exchange, operation, request)` sends one
request through middleware, retries, dry runs, tracing and the operation's credentials
(authenticators run last and can refresh a session); `exchange.decode(value,
operation.result, "$result")` validates the result according to `response_validation`.

```python
from codapact.runtime import HttpRequest, send_authorized


class GwtRpc:
    def __init__(self, exchange):
        self.exchange = exchange

    def call(self, operation, binding, arguments):
        body = encode(binding.options, arguments)  # protocol-specific
        request = HttpRequest("POST", f"{self.exchange.base_url}/app", {}, body)
        response = send_authorized(self.exchange, operation, request)
        return self.exchange.decode(decode(response.body), operation.result, "$result")
```

Register handlers per client (`Client(protocols={"gwt-rpc": GwtRpc})`,
`RuntimeClient(protocols=…)`; values may also be `"module:attribute"` strings, imported on
first use), or bake them into a generated package with Python target options:

```yaml
targets:
  - name: python
    output: gen/python
    options:
      package: acme
      protocols: {gwt-rpc: "acme._gwt:GwtRpc"}     # default handlers of the generated Client
      include: {_gwt.py: handlers/gwt.py}            # hand-written files copied into the package
  - {name: python-cli, output: gen/python}
```

`include` maps a path inside the package to a file relative to `codapact.yaml` (files outside
the project directory are refused). Included files are written and tracked like generated
ones, so the hand-written source stays outside the generated tree and regeneration copies
it again. The same mechanism ships an [auth plugin](#credential-fields) with the CLI
(`cli.auth_plugin: acme._session:provider`). Targets without the `binding:custom`
capability (TypeScript) refuse such contracts before generating.

## Shaping transforms

| Transform | Config |
| --- | --- |
| `server` | `url` (replaces the default server, e.g. a `https://{your-domain}` template), `prefix` (a path such as `/wiki/api/v2` moved from the server URL into every operation path, so `--base-url` is the site) |
| `select` | `include`, `exclude`: lists of globs or `{name, path, tag, kind, namespace, read_only}` selectors. Names match in any spelling (`getIssue`, `get-issue` and `get_issue` are the same); a glob starting with `/` or a method (`GET /rest/api/3/issue/*`) matches the HTTP path |
| `rename` | `operations`, `types`, `fields` (`Type.field`), `params` (`op.param`), `patterns` (`[{match: regex, replace: template}]` over operation names; explicit `operations` entries win); wire names are kept |
| `group` | `by: tag` or `by: path`, and/or `operations: {glob: dotted.namespace}` |
| `paginate` | `operations`, `style: cursor` (`param`, `next`), `offset` (`param`, `limit_param`), or `page` (`param`, default `page`; `start`, default `1`), and `items` (`$` when the result itself is the list). Operations matched only by a pattern that lack the param or items are skipped with a warning; operations named exactly are errors |
| `defaults` | `params: {"op_glob.param": value}`; the param becomes optional and the runtime sends `value` when it is omitted |
| `headers` | `{SELECTOR: {Header-Name: value}}` (selectors as in `select`, e.g. `addAttachment` or `POST /rest/*`). Each header becomes an optional header param defaulting to the value, so it is always sent unless overridden (`--x-atlassian-token`); a header the contract declares just gets the default. For vendor headers contracts mention only in prose, such as Jira's `X-Atlassian-Token: no-check` |
| `auth` | `schemes: {name: SPEC}` ([auth specs](#auth-specs)), `operations` (default all), `optional` (send credentials when set, call anonymously otherwise), `alternative` (add the schemes as another way to sign in instead of replacing the declared ones; anonymous access stays last). Without `alternative`, schemes no operation uses afterwards are removed. For contracts that omit or under-declare `securitySchemes`; re-declaring an identical scheme is allowed |
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

`python-readme` takes no options: it writes only `README.md`, in the format below, for the
first `python` target, usually into the repository root (`output: .`).

## README

The `python` target writes `README.md` next to the package, and `python-readme` writes the
same text wherever it is pointed. Every generated README has one format, in this order:

| Section | Shown when | Content |
| --- | --- | --- |
| Title | always | The distribution name |
| Badges | `readme.repository` is set | CI status, PyPI version, Python versions, license, API version, generating Codapact version |
| Introduction | always | `readme.about`, else "Typed Python client and `PROGRAM` command-line tool for the [NAME](docs)" |
| API source | `readme.docs` or `source.upstream` is set | Link to the API, the pinned contract (linked on GitHub when hosted there) and its upstream, the contract's `info.version`, its SHA-256, and the Codapact version: the exact input the code was generated from |
| Run | a CLI is generated | `uvx` commands (from PyPI, or `--from` the package folder when not published); never `uv tool install` |
| Credentials | the API has auth schemes | Environment variables per scheme, `cli.auth.SCHEME.docs`, `auth login`, `doctor` |
| Use | always | `readme.examples`, `describe --brief`, library usage |
| Extra sections | `readme.sections` | Your own sections, verbatim |
| Development | always | `codapact sync --check` / `sync` (with an upstream), `generate`, offline and live tests, pinned to the generating Codapact version |

| `readme` key | Default | Meaning |
| --- | --- | --- |
| `name` | contract title | Product name of the API, e.g. `YNAB API` |
| `about` | generated sentence | Replaces the introduction, e.g. for a service without a published API |
| `docs` | none | Public API documentation URL. Leave it out, and `source.upstream`, when the service publishes no API: the README then has no API source section or API badge |
| `repository` | none | GitHub `owner/name` (or its URL); turns on badges and repository links |
| `ci_workflow` | `ci.yml` | Workflow file the CI badge shows |
| `pypi` | `true` with `repository`, else `false` | Whether the package is on PyPI: PyPI badges and `uvx DISTRIBUTION` instead of `uvx --from PATH` |
| `badges` | all | Subset and order of `ci`, `pypi`, `python`, `license`, `api`, `codapact` |
| `examples` | none | Command lines after the program name, shown under Use |
| `sections` | none | List of `{title, body}` (Markdown) added after Use |

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
| JSON style | none | `<PREFIX>_JSON_STYLE=auto\|pretty\|compact` | `json_style` |
| Error format | `--error-format text\|json` | `<PREFIX>_ERROR_FORMAT` | `error_format` |
| Credentials | none | `<PREFIX>_<SCHEME>`; basic uses `_USERNAME`/`_PASSWORD`; full chain under [Authentication](#authentication) | `auth_plugin`; secrets are never read from the file |
| Option defaults | the option itself | none | `defaults: {plan_id: "…"}` (option names, `_` or `-`) |
| Saved-login profile | `--profile NAME` | `<PREFIX>_PROFILE` | `profile`; otherwise the default from `auth use` ([Profiles](#profiles)) |

The settings file is `--config PATH` or `$XDG_CONFIG_HOME/<program>/config.json`.
`<PREFIX>` is `cli.env_prefix` when set (upper snake case, e.g. `ACME`), else the program
name in upper snake case (`cli.program: my-tool` gives `MY_TOOL`). `<program> --help` lists
the credential variables for the contract's auth schemes.

Global options work before or after the command: `--base-url`, `--timeout`, `--profile`, `--config`,
`-o/--output`, `--query PATH` (`data.items[].name`, `data.items[0]`; with `--all` the
result is the item list, so `[].name`; a path that matches nothing fails with exit 2 and lists the keys that exist), `--dry-run`, `-v/--verbose`, `--error-format`,
`--allow-private`, and `--response-validation`. Command help lists them in one line;
`<program> --help` describes each.
`table` renders a list of objects, unwrapping single-list envelopes such as `{"data": {"items": [...]}}`.
JSON is indented on a terminal and compact (no whitespace) when stdout is a pipe or file,
where an agent or script reads it; `<PREFIX>_JSON_STYLE=pretty` or `compact` fixes one style.
`jsonl` prints one JSON value per line, unwrapping the same envelopes as `table`, and streams
`--all` results page by page. Output to a pipe or file is UTF-8 on every platform.

### Program name

The command is named after the Python distribution that ships it (`distribution`, else
`package` with `-` for `_`), so `uvx acme-api --help` runs it with no `--from`. When no
Python target names one, it is the API's name. `cli.program` overrides both.
Codas this project publishes are named `codapact-<api>` everywhere; see
[Publishing codas](publishing-codas.md#naming).

Renaming a CLI moves its keyring entries and settings directory to the new name. List
the old names in `cli.previous_programs`: a saved login found only under an old name
moves to the new one the first time it is read, `auth logout` removes every copy, and
the settings file and profiles are read from the old directory until the new one
exists. Set `cli.env_prefix` too if the environment variables should keep their names.

### Conventions every generated CLI follows

| Need | How |
| --- | --- |
| Help | `-h`/`--help` on every level; bare `<program>` or `<program> GROUP` prints help (exit `0`). Command help has a usage line with positionals and required options, examples, and a global-options reminder. |
| Version | `-V`/`--version`: `poke 0.1.0 (PokéAPI 2.10.0; codapact 0.4.0)`; the installed distribution's version when it differs from the generated `version` option (so a release tool can bump packaging alone) |
| Discovery for agents | `<program> describe [COMMAND…]`: JSON with every command's arguments (type, required, positional, choices, repeated), `read_only`/`destructive`/`paginated`, `requires_one_of` option groups, environment variables, exit codes, `start` (the steps an agent follows: doctor, credentials, describe, dry run) and `auth` (per scheme: environment variables, `auth login` commands, `docs` on how to get the credential, and whether every command or only some need it). `describe --brief` keeps those plus names, summaries, and usage lines, so one call is enough to start; past 150 commands it lists groups (name, summary, command count) instead, and `describe GROUP --brief` lists a group. Agent skills for a coda point here instead of repeating it (see [Agent skills for codas](../extras/agent-plugin/README.md)). Naming one command adds the JSON Schema of each JSON-valued argument; `--schemas` adds that and the output JSON Schema to every command. Printed to a pipe, `describe` is compact JSON. `--help` names the members of each JSON argument, and examples include one option of each `requires_one_of` group (see [Discovery efficiency](discovery-efficiency/README.md)) |
| Diagnosis | `<program> doctor [--offline]`: version, settings file, base URL and its source, credentials (set or missing, never printed), proxy, connectivity (any HTTP answer counts, a redirect included), and, for schemes with `cli.auth.<scheme>.check`, whether the server still accepts the credential (`session: <scheme>`, see [CLI credential chain](#cli-credential-chain)). Missing credentials fail only when every command needs them. Table on a terminal, JSON otherwise; exit `0` ready, `3` credentials missing or rejected, `4` API unreachable, `1` any other failed check |
| Completion | `<program> completion bash\|zsh\|fish\|powershell` prints a script that asks the program for candidates |
| Preview | `--dry-run` prints the HTTP request (method, URL, headers, body) without sending it; credentials appear as `$VARIABLE` placeholders (optional credentials that are not set stay off the request), and destructive commands skip confirmation |
| Tracing | `-v`/`--verbose` logs each request line and response status, size, and time to stderr, masking credential values |
| Mistakes | unknown commands, choices, and options get "did you mean" hints; leftovers are reported by the command they were given to; API failures name the command typed (`pets get failed with HTTP 404`) |
| Machine-readable errors | `--error-format json` prints one `{"error": {"type", "message", "exit_code", "status", "hint", "details"}}` object on stderr; contract warnings become `{"warning": …}` lines |

| Credentials | `<program> auth login\|logout\|status\|token\|profiles\|use`; see [Authentication](#authentication) |

`describe`, `doctor`, `completion`, and `auth` are skipped when the API has a top-level command
of the same name; `--version`, `--dry-run`, `--verbose`, `--error-format`, and `--profile` are
reserved, so API parameters with those names become `--arg-version` and so on.

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
| Bespoke | `Authenticator.authenticate(request, scheme) -> request`, optional `refresh(scheme) -> bool` and `expired(response, scheme) -> bool` | Request signing (HMAC, AWS SigV4), token exchange, browser sign-in sessions, anything else |

An `Authenticator` runs last, after middleware, so a retried request is signed again. On
HTTP 401 the runtime calls `refresh()` and, if it returns true, sends the request once more.
Servers that reject a session some other way (a redirect to a sign-in page, an HTML login
form, a vendor error code) are recognised by `expired()`, which sees every response: when it
returns true the runtime refreshes and resends the same way, and if the session is still
rejected the call fails with `AuthError` (CLI exit code 3).

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

Every spec map also takes `docs` (for `custom` it replaces the description). OpenAPI `oauth2` (including 3.2 `deviceAuthorization`) and
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
5. Browser session, for schemes with `cli.auth.<scheme>.browser` (see
   [Browser sign-in](#browser-sign-in)).
6. Saved login from `<program> auth login`.

`cli.auth: {SCHEME: {client_id, scopes, flow, audience}}` bakes public OAuth settings into
the CLI (no secrets); `fields` declares [credential fields](#credential-fields); `docs` tells a
person how to get the credential (shown in `describe` under `auth`, e.g. "Create a personal
access token under Account Settings > Developer Settings"); `check` names an operation that
needs no arguments (any spelling, e.g. `getCurrentUser`) which `doctor` calls to prove the
saved credential or session still works: a 401, 403, redirect or `expired()` fails the
`session: <scheme>` check with exit code 3.
`auth login` picks a flow in this order: `--flow`, then client credentials when a secret is
set, then device code, then authorization code with PKCE.
`--with-token` reads a static token from stdin; `--username` saves basic credentials.
`auth status` shows each scheme's source; `auth token` prints what a command would send;
`doctor` reports the same, never printing secrets.

### Browser sign-in

For services where API tokens are disabled, a scheme can use cookies from a real browser
session instead. Declare a `custom` scheme as an alternative and give it `browser`
settings:

```yaml
shape:
  - auth: {schemes: {session: {custom: "Browser sign-in"}}, alternative: true}
cli:
  auth:
    session:
      browser:
        login_url: "{base_url}/"                 # page that ends signed in on the API host
        cookies: [tenant.session.token]          # names that prove the session
        headers: {X-Atlassian-Token: no-check}   # sent with every request (optional)
        domain: example.atlassian.net            # cookie host (default: login URL host)
        signin_hosts: ["id.example.com"]         # sign-in hosts a rejected session redirects to
```

* `<program> auth login --scheme session` opens a browser window; the person signs in
  (SSO, MFA and all) and the CLI saves the domain's cookies in the credential store once
  every named cookie is present and the page is back on the API host (5 minute limit).
* Each request sends the cookies a browser would send to that URL (domain, path, Secure
  and expiry rules) and the configured headers. A session only goes to the origin that
  issued it; after changing `--base-url`, sign in again.
* A 401, or a redirect to a sign-in page, renews the session without a window from the
  same browser profile (identity providers keep their own longer login) and resends once.
  A sign-in page is a host in `signin_hosts`, or, when that is unset, an off-host URL
  that looks like one (`login`, `sso`, `saml`, `oauth`...); other redirects, such as a
  303 to a result, are never treated as expiry, so writes are not replayed.
  When renewal fails the command exits 3 and asks for `auth login` again.
* Playwright drives the browser: install the `browser` extra
  (`pip install codapact[browser]`, or ship `playwright` with the coda). It uses an
  installed Chrome, then Edge, then Playwright's Chromium; `<PREFIX>_BROWSER=chrome|msedge|chromium`
  picks one.
* Each scheme and profile has its own browser directory under
  `$XDG_STATE_HOME/<program>/browser/<profile>/<scheme>` (`%LOCALAPPDATA%` on Windows);
  `auth logout` removes it and reports when it cannot. With
  `<PREFIX>_CREDENTIAL_STORE=memory` the browser profile is temporary as well.

### Credential fields

When a scheme needs several values that belong together (a login URL, username, password and
cookie name for a browser sign-in; a key ID and secret for request signing; an API key plus a
tenant), declare them as fields and let an auth plugin use them:

```yaml
cli:
  auth_plugin: browser_cookie_auth:provider
  auth:
    portal:
      fields:
        login_url: {help: page that sets the cookie}
        username: {}
        password: {secret: true}
        cookie_domain: {optional: true}
```

| Piece | Behaviour |
| --- | --- |
| `auth login --scheme portal` | Prompts for each field (secret ones hidden, saved values offered as defaults) and saves them together; `--field NAME=VALUE` sets a non-secret field without prompting |
| Environment | `<PREFIX>_<SCHEME>_<FIELD>` (e.g. `ACME_PORTAL_PASSWORD`) wins over a saved value |
| Plugin | `context.fields("portal")` returns `{name: value}`; missing required fields raise an error naming the variables and `auth login`. `context.session_key("portal")` is a store key for what the plugin obtains (e.g. a session cookie) |
| `auth status` | Shows where each field comes from (`set (VAR)`, `saved`, `missing`), never the value |
| `auth logout` | Forgets the fields and the plugin's cached session |

The sign-in itself (multi-step forms, SSO, redirects, MFA) stays in the plugin; fields only
supply and store its inputs. Field names are lower snake case; `secret`, `optional` and `help`
are the only keys.

Saved credentials and tokens go to the OS keyring when one is usable, one entry per profile
and scheme named `codapact:<program>:<profile>:<scheme>` (user name `<profile>:<scheme>`; for
example `codapact:ynab:default:bearer` in Windows Credential Manager), otherwise to a
`0600` JSON file at `$XDG_CONFIG_HOME/<program>/credentials.json` (keys `<profile>:<scheme>`).
`<PREFIX>_CREDENTIAL_STORE=keyring|file|memory` forces one. Logins saved before profiles
existed (keyring service `<program>` before 0.6.1, `codapact:<program>:<scheme>` in 0.6.1)
belong to the `default` profile and move to its name the first time they are read.

### Profiles

Several saved logins per CLI: different accounts, or tokens with different access levels.

| Need | Command |
| --- | --- |
| First login | `auth login` saves to profile `default`, which becomes the default |
| Another login, named automatically | `auth login --new` (`profile-2`, `profile-3`, …) |
| Another login, named by you | `auth login --profile work` (creates `work`, or replaces its login) |
| List | `auth profiles`: name, whether it is the default, which schemes are saved, base URL, username |
| Change the default | `auth use work` |
| Use one for a single command | `--profile work` on any command, or `<PREFIX>_PROFILE=work` |
| Remove | `auth logout --profile work` (without `--scheme` it also forgets the profile) |

Selection order: `--profile`, `<PREFIX>_PROFILE`, the settings file's `profile`, the default
set by `auth use` (stored in `$XDG_CONFIG_HOME/<program>/profiles.json`, no secrets), then
`default`. Naming a profile that does not exist fails, except for `auth login`, which creates
it. `<PREFIX>_<SCHEME>` variables, credential commands and OAuth client settings still come
before a profile's saved login. `auth status` and `doctor` show the profile in use and the
exact keyring entry or file key a saved login came from.

Each profile also keeps non-secret settings in `profiles.json`: `base_url`, the `scheme` it
last signed in with, and per scheme the username (`username.<scheme>`). `auth login` saves
them, merging into the file as it is on disk so a login in another terminal is not lost. Commands read the base URL in this
order: `--base-url`, `<PREFIX>_BASE_URL`, the profile, the settings file, the contract
(`doctor` names the source, e.g. `profile work`).

#### `auth login` on a terminal

With stdin and stderr both terminals, `auth login` asks for what the command line leaves out:

1. **Base URL**, when the contract's server is a placeholder (`{site}`, `your-domain.…`,
   `example.…`) or the profile already has one. `site.example` becomes
   `https://site.example`; a URL with a user name, password or fragment is refused. The
   default is the profile's URL, else the settings file's. Skipped when `--base-url` or
   `<PREFIX>_BASE_URL` is set. Moving a profile with saved logins to another site asks first
   and forgets them (without a terminal it refuses): use `auth login --new` for a second
   site.
2. **Scheme**, from a numbered list showing the first sentence of each
   `cli.auth.<scheme>.docs`. Skipped with `--scheme` or when there is only one.
3. **The scheme's own prompts.** Basic asks for the username, then whether to save the
   password; a browser scheme asks for a username to fill in on the sign-in page (optional).

Saved values are the defaults next time (`[shown in brackets]`; press Enter to keep). Without
a terminal, `auth login` behaves as before: `--scheme` is required when there are several.

#### Keeping a password out of the keyring

On a terminal, a basic login asks whether to save the password, and the default answer is No
(it may be a directory or identity-provider password); `cli.auth.<scheme>.save_secret: true`
in `codapact.yaml` makes Yes the default, for schemes whose secret is an API token. Scripts
(no terminal, or `--with-token`) save it unless `save_secret: false`. `auth login
--no-save-secret` (or answering `n`) saves only the username for a basic scheme, replacing
any password saved before; the username belongs to that scheme alone. Later commands then take the password from
`<PREFIX>_<SCHEME>_PASSWORD` or, on a terminal, ask for it (hidden) the first time a request
needs it, and keep it in memory for that process only, exactly as typed. `auth token
--scheme NAME` asks too. Without a terminal or the variable the
command exits 3 and says so. `auth status` shows `saved username NAME (password asked when
needed)`. `auth logout --scheme NAME` forgets that scheme's username too. Declared `fields` are always
saved; `--save-secret` does not apply to them.

Browser schemes never store a password: the identity provider's own session lives in the
per-profile browser directory. The scheme's saved username is typed into the first empty
username or email box of each sign-in page, never over what you typed, and only on trusted
pages: the site itself, its `signin_hosts`, or (without `signin_hosts`) hosts that look like
sign-in pages, over HTTPS.

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

## Call hooks and CLI extensions

A call hook changes the arguments and results of operation calls; both methods are optional:

```python
class Markdown:
    def before(self, operation, arguments):  # before validation and encoding
        return arguments

    def after(self, operation, result):  # on the decoded result
        return result
```

`before` hooks run in order and `after` hooks in reverse; on paginated calls `after` runs
on each item (paging reads cursors from the unmodified page). Subclasses of `CallHook` and
`CliExtension` inherit pass-through defaults. Pass them as
`Client(hooks=[...])` or `RuntimeClient(hooks=[...])`, as objects, zero-argument factories,
or `"package.module:attribute"` strings.

`cli.extensions` lists `"package.module:factory"` strings (ship the module with the Python
target's `include`). Each factory receives the `CliRuntime` and returns an object with any of:

| Method | Purpose |
| --- | --- |
| `add_options(group, hidden)` | add global options; use `default=argparse.SUPPRESS`, and hide help when `hidden` |
| `hooks(args)` | call hooks for this invocation |
| `notes()` | lines added to `describe` notes so agents learn about the extension |

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
