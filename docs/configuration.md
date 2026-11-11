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
cli: {program: petstore}
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
| both | `strict` | `true` | Unsupported features are errors; when `false`, they degrade with a warning |
| `graphql` | `name` | `GraphQL API` | API name |
| `graphql` | `version` | empty | API version |
| `graphql` | `url` | none | Endpoint; becomes the default server |
| `graphql` | `auth` | none | `bearer`, `basic`, or `{api_key: HEADER}`; required on every operation |
| `graphql` | `depth` | `1` | Object nesting synthesized in schema mode (0–8) |

GraphQL with `documents` generates one operation per document operation with exact result
types. Without documents, it generates one operation per root field.

## Shaping transforms

| Transform | Config |
| --- | --- |
| `select` | `include`, `exclude`: lists of globs or `{name, tag, kind, namespace, read_only}` selectors |
| `rename` | `operations`, `types`, `fields` (`Type.field`), `params` (`op.param`), `patterns` (`[{match: regex, replace: template}]` over operation names; explicit `operations` entries win); wire names are kept |
| `group` | `by: tag` or `by: path`, and/or `operations: {glob: dotted.namespace}` |
| `paginate` | `operations`, `style: cursor` (`param`, `next`) or `offset` (`param`, `limit_param`), `items` |
| `defaults` | `params: {"op_glob.param": value}`; the param becomes optional and the runtime sends `value` when it is omitted |
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
| Private network | none | `<PREFIX>_ALLOW_PRIVATE` | `allow_private` |
| Credentials | none | `<PREFIX>_<SCHEME>`; basic uses `_USERNAME`/`_PASSWORD` | not stored |
| Option defaults | the option itself | none | `defaults: {plan_id: "…"}` (option names, `_` or `-`) |

The settings file is `--config PATH` or `$XDG_CONFIG_HOME/<program>/config.json`.
`<PREFIX>` is the program name in upper snake case. `<program> --help` lists the
credential variables for the contract's auth schemes.

Global options work before or after the command: `--base-url`, `--timeout`, `--config`,
`--output json|yaml|table|raw`, and `--query PATH` (`data.items[].name`, `data.items[0]`).
`table` renders a list of objects, unwrapping single-list envelopes such as `{"data": {"items": [...]}}`.

Commands for HTTP `DELETE` operations, or operations named `delete_*`/`remove_*`/`destroy_*`/`purge_*`,
ask for confirmation on a terminal and require `--yes` otherwise.

Validation errors name the command-line option (`--amount: expected an integer`).

Exit codes: `0` success, `1` API error, `2` usage or validation, `3` auth (missing credentials or
HTTP 401/403), `4` transport.

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
