# Generation manifests and runtime configuration

CodaPact separates generation settings, the generated command definition, and
runtime configuration. Each is versioned data; none imports code, executes shell
commands, or evaluates templates.

## Generate from reusable values

```yaml
version: 1
contract:
  source: pets.openapi.yaml
  type: openapi
  allow_private: false
output:
  directory: generated/pets
client:
  language: python
  package: pets_client
cli:
  style: argparse
  slug: pets
  definition: null
runtime:
  config_directory: null
  defaults:
    timeout: 15
    allow_private: false
    policy:
      deny: []
```

```console
codapact generate --manifest pets.codapact.yaml --output builds/pets --runtime-timeout 20
codapact generate --manifest pets.codapact.yaml --set 'runtime.defaults.policy.allow=["get_pet"]'
```

The [generation schema](../src/codapact/schemas/generation.v1.json) rejects unknown
keys, unsupported source/client/CLI types, unsafe YAML tags, aliases, duplicate
keys, and non-JSON values. Only local manifests are supported. The original
`codapact generate api.yaml --output generated --package pets_client` still works.

Precedence is built-in defaults, manifest, explicitly supplied named flags, then
repeated `--set FIELD=JSON` values in order (last wins). Omitted flags preserve
manifest values. Maps merge recursively; an explicitly empty nested map resets
that map. Lists, scalars, `false`, and empty lists replace existing values. Null
is supported only for CLI definition, config directory, base URL, and policy
allow: these remove that override. An empty allow list denies every operation.
An empty map at the root adds no overrides. `--set` accepts JSON, not YAML.

| Manifest field | Named generation override | Default |
| --- | --- | --- |
| version | `--manifest-version` | 1; manifest must declare 1 |
| contract.source | positional source, or `--set contract.source=JSON` | Required |
| contract.type | `--contract-type` | openapi |
| contract.allow_private | `--allow-private` / `--no-allow-private` | false |
| output.directory | `--output` | generated |
| output.dry_run | `--dry-run` / `--no-dry-run` | false |
| client.language | `--client-language` | python |
| client.package | `--package` | slug with underscores, plus _client |
| cli.style | `--cli-style` | argparse |
| cli.slug | `--slug` / `--prefix` | Package with hyphens, otherwise api |
| cli.definition | `--cli-definition` | null; commands derived from operations |
| runtime.config_directory | `--runtime-config-dir` | null; XDG discovery |
| runtime.defaults.base_url | `--runtime-base-url` | Contract server |
| runtime.defaults.timeout | `--runtime-timeout` | 15 seconds per socket operation |
| runtime.defaults.allow_private | `--runtime-allow-private` / `--no-runtime-allow-private` | false |
| runtime.defaults.policy.allow | `--runtime-policy-allow JSON` | All except denied operations |
| runtime.defaults.policy.deny | `--runtime-policy-deny JSON` | Empty list |
| selection.version | `--selection-version` | 1 |
| selection.plugin | `--selection-plugin` | operations |
| selection.preset | `--preset` | all |
| selection.include | `--include JSON` | Empty list: include all |
| selection.exclude | `--exclude JSON` | Empty list: remove none |

All fields, including whole maps and explicit nulls, also support `--set`.
See [endpoint selection](selection.md) for rule composition, domain/read profiles,
strict unmatched-selector handling, and the inspectable dry-run report.
Relative source, output, and CLI-definition paths are anchored to the manifest
folder, including relative flag overrides. Without a manifest they use the
invocation directory. Absolute paths stay absolute. Contract HTTP(S) URLs remain
absolute URLs; they are never joined to a manifest location. Other URI schemes
and remote CLI definitions are rejected. Remote contract requests use the same
bounded transport with private-network opt-in and redirects disabled.

Runtime config directories embedded in generated output must be absolute or
start with `~/`; they are expanded on the runtime machine. Prefer null or `~/`
to avoid embedding a build-machine path. Generation defaults never contain auth
credentials. Use environment references or local runtime credential files.

## Runtime discovery and precedence

A CLI generated with slug `pets` reads `config.json` and `auth.json` from
`$XDG_CONFIG_HOME/pets` when XDG_CONFIG_HOME is nonempty and absolute; otherwise
it uses `~/.config/pets`. Windows follows this same rule: a fully qualified drive
or UNC root is absolute; relative or drive-relative XDG roots are ignored. It
does not substitute AppData. Slugs are lowercase letters/digits/hyphens, start
with a letter, are at most 50 characters, and reject traversal/reserved names.

Directory precedence is generated XDG default, generation manifest's
runtime.config_directory, then generated CLI `--config-dir`. Runtime
`--config-dir` may be relative to the invocation directory. The CLI never
creates these directories or files.

Ordinary setting precedence is generated runtime defaults, local config.json
(or explicit `--config PATH`), then supplied `--base-url`, `--timeout`, and
`--allow-private` / `--no-allow-private`. Nested policy maps follow the merge rules
above. Local config.json accepts `version: 1`, base_url, timeout, allow_private,
and policy.allow/policy.deny. It rejects unknown keys and auth data.

`--auth-file PATH` replaces auth.json; `--no-auth-file` disables credential-file
discovery. These flags conflict. Missing discovered files are allowed; missing
explicit files fail. Auth entries take precedence over legacy environment auth
references by security-scheme name. A malformed auth file fails closed. Files
are bounded to 64 KiB; directories, links, and junctions are rejected. Reads
must be from trusted directories: this is not a sandbox against a concurrent
local process that can replace files while they are opened.

For backward compatibility, explicit `--config PATH` accepts JSON/YAML and the
original `auth` environment-reference mapping. Automatic config.json/auth.json
require JSON. Old generated libraries without a runtime profile retain explicit
configuration behavior. Programmatic clients apply generated ordinary defaults
and explicit settings, without reading ambient config/auth files.

Example `~/.config/pets/config.json`:

```json
{"version":1,"timeout":15,"policy":{"deny":["delete_pet"]}}
```

Example auth.json using environment references:

```json
{"version":1,"credentials":{"token":{"env":"PETS_TOKEN"}}}
```

Auth keys match the contract's scheme names. Each entry is exactly one of
`{"env":"NAME"}`, `{"username_env":"USER_ENV","password_env":"PASS_ENV"}`,
`{"value":"<local-secret>"}`, or `{"username":"<user>","password":"<local-secret>"}`.
Literal credentials are supported only in the user's runtime auth.json.
Plaintext auth.json is readable by anyone with filesystem access: prefer
references, and protect literal files with owner-only permissions (POSIX 0600
file/0700 directory; appropriate owner-only Windows ACLs). CodaPact never writes
secrets or changes permissions. Errors omit credentials and HTTP response bodies.

`CredentialStore` and `StoredAuth` in codapact.profiles provide explicit Python
dependency injection for alternate stores. JsonFileStore is the implemented file
provider; EnvironmentAuth remains available. Windows Credential Manager and
system keyrings are future providers. No config string imports arbitrary code.
API security alternatives are OR; schemes within an alternative are AND.
Client policy is a local guard; the server remains the authorization authority.

## Commands and scriptable results

The [CLI definition v1](../schemas/cli.v1.schema.json) assigns stable command names:

```json
{"version":1,"commands":[{"name":"show-pet","operation":"get_pet"}]}
```

Every operation must be covered, names unique and safe. Without a definition,
operation underscores become hyphens. Parameters use `--arg-<parameter-name>`;
required parameters are required flags. Non-string values are JSON scalars.
Bodies use `--body '{"id":1,"name":"Miso"}'`. Global options precede the command.
The CLI never prompts. JSON goes to stdout, including `null` for empty responses;
`--pretty` indents. Errors go to stderr.

| Exit | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Arguments, configuration, request validation, or generation failure |
| 3 | Authentication unavailable or client policy denied |
| 4 | Network, TLS, redirect, or transport limit failure |
| 5 | HTTP API failure |
| 6 | Response violates declared contract |
