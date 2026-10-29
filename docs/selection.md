# Specialized clients and CLIs

Select operations after translation into the shared IR and before either
generator runs. The library and CLI receive the same filtered contract. Omit
`selection` to keep every operation, preserving the existing default surface.

```yaml
version: 1
contract:
  source: domains.openapi.yaml
output:
  directory: generated/pets-read
client:
  package: pets_read
cli:
  slug: pets-read
selection:
  version: 1
  plugin: operations
  preset: read
  include:
    - tags: [Pets]
      paths: ['/pets*']
  exclude: []
```

The [runnable domain profile](../examples/pets-read.codapact.yaml) also shows
stable custom command names. Run it from any directory:

```console
codapact generate --manifest examples/pets-read.codapact.yaml --dry-run
codapact generate --manifest examples/pets-read.codapact.yaml
```

The `read` preset selects **GET, HEAD, and OPTIONS** by their method names.
It does not prove an endpoint is read-only or safe: a server can mutate data
when handling GET. Filtering reduces generated methods and commands; it does
not restrict server access, authorize a caller, or replace server enforcement.
Selected operations retain authentication requirements and runtime policy hooks.

## Rules and matching

- `preset: all` is the default. `read` intersects the inclusion result.
- An empty or omitted `include` means all operations. Otherwise, include rules
  combine with OR. Fields inside one rule combine with AND; values inside a
  field combine with OR.
- `exclude` rules combine the same way and always win over inclusion or presets.
  An empty exclude list removes nothing.
- `operations` contains exact, case-sensitive normalized operation IDs.
- `methods` contains method names, matched case-insensitively and recorded in
  uppercase. Syntax permits method tokens; an unknown/unmatched token fails.
- `paths` contains case-sensitive, whole-path globs. Only `*` is a wildcard:
  it matches zero or more characters **including slashes**. `/pets/*` matches
  `/pets/42` and `/pets/a/b`, but not `/pets`. `/pets*` matches all three.
  Dots and `{pet_id}` are literal. `?`, bracket classes, backslashes, query/hash
  markers, spaces, and control characters are rejected. No regular expressions
  or code execution are supported.
- `tags` contains exact, case-sensitive domain labels. Any listed label suffices.
  Operations without tags do not match a tag rule. OpenAPI operation tags are
  normalized to sorted unique labels; other adapters can populate the same IR
  field without changing this selector.

For example, this keeps Pets GETs and Billing GETs, then removes one operation:

```yaml
selection:
  preset: read
  include:
    - tags: [Pets]
      methods: [GET]
    - tags: [Billing]
      methods: [GET]
  exclude:
    - operations: [list_invoices]
```

Every individual selector value and every whole AND rule must match at least
one operation in the **full translated contract**. This catches typos inside a
partially matching list and contradictory combinations. A valid exclusion can
match an operation outside the current include/preset surface and have no effect.
Include/exclude conflicts remove the operation and are recorded as `excluded`.
If the final result is empty, generation fails without writing output.

Errors identify a rule, field, and value index without echoing untrusted values.
Unknown keys, duplicate values (including `GET` plus `get`), empty rules/value
lists, invalid versions, and unsupported presets fail clearly. Lists are bounded
to 64 rules per side, 64 values per field, and 256 total selector values. IDs
are at most 80 characters, methods 20, tags 128, and path patterns 512.

## Overrides and inspection

Generation precedence remains defaults, manifest, explicit named flags, then
ordered `--set FIELD=JSON` assignments. Omitted flags preserve manifest values.
Lists replace instead of append. An empty selection map resets all filters;
an empty include list restores all candidates subject to preset/exclusion.

| Field | Named override | Default |
| --- | --- | --- |
| selection.version | `--selection-version` | 1 |
| selection.plugin | `--selection-plugin` | operations |
| selection.preset | `--preset` | all |
| selection.include | `--include JSON` | Empty list: include all |
| selection.exclude | `--exclude JSON` | Empty list: remove none |
| output.dry_run | `--dry-run` / `--no-dry-run` | false |

Every field in the table also supports `--set`, including whole maps and empty
lists. Replace rules as complete JSON arrays; dotted array-index paths are not
supported:

```console
codapact generate api.yaml --preset read --include '[{"tags":["Pets"]}]'
codapact generate --manifest profile.yaml --set 'selection.include=[{"operations":["get_pet"]}]'
codapact generate --manifest profile.yaml --set 'selection={}' --dry-run
```

Generation owns `codapact-selection.json` at the output root. It records the
normalized definition, matched IDs per rule, selected methods/paths/tags/security
requirements, excluded IDs with reasons, and counts. It contains no timestamps,
source URLs, machine paths, or resolved credentials. Operations and match sets
are sorted by ID. Rule/value order is retained for diagnostics; reordering them
can change the audit report without changing the selected operation set.

Dry-run prints this report as JSON. Built-in generation and ownership preflight
run, including checks for unowned/modified output files, but no directories or
files are written. Remote contract loading still occurs; API operations and auth
providers are not invoked. Programmatic `generate(...)` returns the same report:

```python
from pathlib import Path
from codapact.project import generate

report = generate(
    "api.yaml",
    Path("generated"),
    package="pets_read",
    selection={"preset": "read", "include": [{"tags": ["Pets"]}]},
    dry_run=True,
)
```

## Dependencies, definitions, and regeneration

IR v1 resolves supported schemas inline before selection. Nested request,
response, and parameter schemas remain attached to selected operations. The
auxiliary named-type catalog is conservatively retained; generated Python types
are emitted from selected operations only. All security schemes referenced by
any selected OR/AND alternative remain; unused security schemes are removed.

The entire source is loaded, translated, and validated first. Selection cannot
hide malformed or unsupported features in excluded source operations. External
or recursive references remain unsupported under the existing source limits.

A version 1 custom CLI definition may cover the full contract or only the
selected operations. Known unselected commands are removed; selected operations
must still be covered. Unknown operation IDs and duplicate command names fail,
including duplicates in commands that would be removed. Surviving names and
aliases are preserved. Parameter-flag collisions are checked on selected
operations, not on excluded CLI surfaces.

Regeneration uses the existing SHA-256 ownership rules. Removed methods/types
are regenerated from the filtered contract, obsolete owned package files are
removed after package renames, and unrelated user files remain untouched.
The selection report itself is owned; a user-created or edited report is not
silently overwritten. Multi-file updates retain the existing interruption and
concurrent-generation limitations.

`Selector.select(Contract, definition) -> SelectionResult` is a versioned plugin
contract. The registry checks API versions and capability requirements. The
built-in selector remains available to existing programmatic registries. Custom
selectors are registered explicitly in trusted application code; configuration
never imports them. The generation manifest currently permits the implemented
`operations` plugin. Alternate sources using IR v1 need no new filter engine.
Selector results must retain a nonempty subset of unchanged operations and
preserve contract metadata, the type catalog, and required security schemes.
Unused security schemes may be pruned. Rewriting endpoints or authentication
belongs outside selection and is rejected by orchestration.
