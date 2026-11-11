---
type: explanation
status: active
authority: normative
criticality: high
review:
  status: unreviewed
---

# Architecture

CodaPact turns API contracts into libraries, then turns libraries into CLIs. Every stage
is a pure function over an explicit, serializable intermediate artifact. Each artifact can
be inspected (`codapact inspect …`), tested in isolation, and replaced without touching the
layers around it.

## Principles

1. **Semantics before protocol.** An operation's meaning (name, params, result, errors,
   auth, pagination) is separate from how it travels (`HttpBinding`, `GraphQLBinding`).
   Everything above the model works for any protocol that can be bound.
2. **The CLI is derived from the library, not from the contract.** The command model is
   built from the library surface, so a CLI command always maps to exactly one library
   method, with the same names, grouping, and flattening.
3. **Generated code is a facade.** Behavior (retries, auth, encoding, validation, paging)
   lives in a versioned runtime. Fixes ship as a dependency bump, not a regeneration.
4. **Customize by configuration and overlays, not by editing output.** Shaping transforms
   rename, group, filter, and annotate the model; the writer refuses to overwrite files it
   does not own.
5. **Diagnostics, not exceptions or prints.** Each stage returns structured diagnostics
   with stable codes and locations. Strict mode turns degradations into errors.
6. **Capabilities are negotiated.** The pipeline computes which features a model uses and
   refuses to run a target that does not declare support for all of them.

## Layers

| Layer | Package | Input → output | Swappable unit |
| --- | --- | --- | --- |
| L0 acquire | `sources.loader` | path/URL → `SourceDocument` | `Fetcher` |
| L1 sources | `sources.openapi`, `sources.graphql` | document → `Api` | `Source` |
| L2 model | `model` | `Api` (+ JSON, validation, migrations) | none (the contract between layers) |
| L3 shaping | `shaping` | `Api` → `Api` + audit report | `Transform` |
| L4 surface | `surface` | `Api` → `Surface` (namespaces, methods, params) | `derive_surface` options |
| L5 commands | `commands` | `Api` + `Surface` → `CommandTree` | `derive_commands` |
| L6 targets | `targets.*`, `plugins` | `GenerationRequest` → files | `Target`, external plugin |
| L7 write | `pipeline.writer` | files → disk + ownership manifest | `plan`/`write` |
| runtime | `runtime` | used by generated Python code | `Transport`, `Middleware`, `AuthProvider`, `ProtocolAdapter` |

`pipeline` is the only composition root. `pipeline.defaults.default_registry()` is the one
place that names concrete implementations.

### Dependency rules

Enforced by `tests/unit/test_architecture.py` (AST import scan):

- `core` depends on nothing in the package.
- `model` depends only on `core`.
- `sources`, `shaping`, `surface` depend only on `core` and `model` (`commands` also on
  `surface`).
- `runtime` depends only on `core` and `model`, so it can be split into its own
  distribution.
- Each target depends on `plugins.protocol` and the layers below it, never on another
  target or on `pipeline`.
- The root `codapact` package exports only `__version__`.

## The semantic model (L2)

- Types: `Record`, `Enum`, `Union` (optionally discriminated), `Alias`; expressions
  `Primitive`, `Ref`, `ListOf`, `MapOf`, `Nullable`, `Literal`. Fields carry a `wire_name`,
  so renames never change what goes on the wire.
- Operations: params, result, named error cases, auth requirement (alternatives of scheme
  sets), pagination (cursor or offset), namespace, tags, and one or more bindings.
- Serialized as canonical JSON with `model_version`. `loads()` runs forward migrations
  first, so artifacts and plugins written against an older version keep working.
- `validate()` checks unique names, resolvable references, identifier syntax, binding
  consistency, declared auth schemes, and pagination parameters. The shaping engine runs it
  after every step.

## Extension seams

| Seam | Protocol | Registered via |
| --- | --- | --- |
| Contract format | `Source.parse(SourceInput) -> SourceResult` | `Registry.add_source` |
| Model rewrite | `Transform.apply(api, config, diagnostics)` | `Registry.add_transform` |
| Language/output | `Target.generate(GenerationRequest) -> GenerationResponse` | `Registry.add_target` |
| Out-of-process target | `<cmd> describe` / `<cmd> generate` over stdin/stdout JSON | `plugin:` in config |
| HTTP sending | `Transport.send(HttpRequest)` | `RuntimeClient(transport=…)` |
| Cross-cutting | `Middleware(request, context, next)` | `RuntimeClient(middleware=…)` |
| Credentials | `AuthProvider.credential(scheme)` | `RuntimeClient(auth=…)` |
| Protocol | `ProtocolAdapter.supports/call` | `RuntimeClient(adapters=…)` |

### Plugin protocol

- `describe` prints `{"name", "capabilities", "needs_commands"}`.
- `generate` reads a `GenerationRequest` (`protocol_version`, model, surface, optional
  command tree, options) and prints a `GenerationResponse` (files + diagnostics).
- File paths are validated (relative, no `..`, no absolute or drive paths) before writing.
- Any language can implement a plugin; it never imports CodaPact internals.

### Capability negotiation

`required_capabilities(api)` yields tokens such as `binding:http`, `binding:graphql`,
`auth:bearer`, `pagination:cursor`, `type:union`, `type:map`, `type:literal`. A target
missing any token fails before generation with `CapabilityError`, rather than emitting code
that silently mishandles the feature.

## Runtime

- `RuntimeClient(api, base_url, auth, transport, middleware, adapters, …)`:
  `call(operation, args)` encodes and validates arguments, picks the adapter for the
  operation's binding, and decodes the result. `paginate()` follows cursors or offsets.
- `SafeHttpClient` (default transport): no redirects, bounded bodies, DNS pinned against
  rebinding, private and plaintext addresses only with explicit opt-in.
- `Retry` middleware: idempotent operations only, exponential backoff, honors
  `Retry-After`.
- Auth: `StaticAuth`, `EnvAuth` (`<PREFIX>_<SCHEME>`), `ChainAuth`. `Credential.__repr__`
  never renders secrets.
- Errors: `ValidationError`, `AuthError`, `ApiError` (status, named error case, decoded
  payload), `GraphQLResponseError`, `TransportError`.

## Lessons from existing tools

| Tool | What it does well | What CodaPact takes |
| --- | --- | --- |
| Smithy | Protocol-agnostic model; traits; projections | Semantics separate from bindings; shaping as projections over one model |
| Fern | Versioned IR consumed by generators | Serialized, versioned model with migrations; generators read IR, not contracts |
| Kiota | Abstractions package separate from HTTP implementation | Runtime seams (`Transport`, `AuthProvider`, `Middleware`) with swappable defaults |
| protoc | Plugins as executables over a request/response protocol | Out-of-process targets with a `describe`/`generate` handshake |
| Stainless | Config-driven resource grouping and naming; hand-edit safety | `group`/`rename` transforms; ownership manifest guards hand-written files |
| Speakeasy | Overlays; retries and pagination declared in config | `paginate` transform; idempotent-only retries with `Retry-After` |
| graphql-codegen | Exact result types per operation document | Selection-specific result types; `__typename`-discriminated unions |
| restish | Generic CLI from a contract at runtime | CLI is a thin layer over the same runtime the library uses |

Deliberate differences:

- One model for REST and GraphQL instead of one generator per protocol.
- CLI derived from the library surface, so the two cannot drift.
- Capability checks instead of best-effort output for unsupported features.

## Testing strategy

Summary; the full strategy, suites, and gates are in [testing](testing.md).

- Each layer has unit tests against hand-built inputs from the layer below.
- Property tests check translation against an independent JSON Schema oracle, and fuzz
  whole contracts through every target.
- Generated Python and TypeScript are compiled, type-checked, and run against loopback
  servers; the two runtimes must put identical requests on the wire.
- Real contracts (PokeAPI, YNAB) are generated in strict mode on every PR; public APIs are
  called nightly.
