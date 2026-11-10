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
| `rename` | `operations`, `types`, `fields` (`Type.field`), `params` (`op.param`); wire names are kept |
| `group` | `by: tag` or `by: path`, and/or `operations: {glob: dotted.namespace}` |
| `paginate` | `operations`, `style: cursor` (`param`, `next`) or `offset` (`param`, `limit_param`), `items` |
| `prune` | none; removes unreachable types (also run by `select`) |

## Surface options

| Option | Default | Meaning |
| --- | --- | --- |
| `flatten_body` | `true` | Expose record body fields as individual method params |
| `trim_namespace` | `true` | `pets.list_pets` becomes `pets.list` unless that collides |

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

The settings file is `--config PATH` or `$XDG_CONFIG_HOME/<program>/config.json`.
`<PREFIX>` is the program name in upper snake case.

Exit codes: `0` success, `1` API error, `2` usage or validation, `3` auth, `4` transport.
