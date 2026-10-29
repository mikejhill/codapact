# Supported contract slice

- OpenAPI 3.0.0–3.0.3, JSON or data-only YAML.
- One absolute HTTP(S) server with no variables; runtime base URL override.
- Unique lowercase snake_case operation IDs; GET, POST, PUT, PATCH, DELETE,
  HEAD, and OPTIONS.
- Scalar path/query/header parameters; simple/form serialization; URL escaping.
- JSON request/response bodies, explicit success statuses, default error schema,
  and empty responses. HTTP failures raise `ApiError` with status and operation.
- Objects, arrays, strings, integers, numbers, booleans, nullable schemas, enums,
  required fields, additional properties, and documented numeric/length bounds.
  Integral JSON numbers map to Python `int` for declared integer fields;
  nonfinite numbers and non-string object keys are rejected.
- Document-local references; security requirement inheritance and overrides.
- HTTP bearer/basic and header API keys; security alternatives use OR and
  schemes within one alternative use AND. Anonymous alternatives are honored.
- Pagination is manual: pass the page/cursor parameter and call again.
  Responses are buffered with a 2 MB limit; streaming is unsupported.

Unsupported schema keywords, composition, recursive/external references,
OpenAPI 3.1, OAuth flows/scopes, cookies, parameter arrays/objects, file uploads,
non-JSON media, server overrides, callbacks, links, response-header declarations,
and streaming are rejected. Descriptions and vendor metadata do not become
executable code. Static types are structural approximations: dictionary extra
keys and enum/numeric constraints are enforced by runtime schemas.

URI-encoded reference fragments are unsupported. JSON Pointer escapes and array
indices must be canonical. GET/HEAD request bodies and JSON bodies declared for
HEAD/204/205 responses are rejected. URLs cannot contain whitespace or control
characters; literal non-ASCII path/query text must be URI-encoded.

| Capability | First iteration |
| --- | --- |
| Contract versions | OpenAPI 3.0.0–3.0.3 |
| Inputs | Local JSON/YAML; public HTTPS; explicit private/HTTP opt-in |
| References | Same-document canonical JSON Pointers |
| Generated library | Typed synchronous Python 3.13+ |
| CLI | Noninteractive argparse; JSON definition v1; JSON stdout |
| Authentication | Bearer, basic, header API key, explicit custom Python hooks |
| Authorization | Client operation policy and custom hooks; server enforces actual permission |
| Security requirements | Inherited/overridden OR alternatives and AND combinations |
| Pagination | Manual page/cursor calls |
| Streaming | Unsupported; bounded buffered responses |
| Other contracts/languages/CLI styles | Explicit extension interfaces; not implemented |

## Safety boundaries

- Parse at most 2 MB, reject YAML aliases, custom tags, duplicate keys,
  non-JSON values, excessive depth, and excessive reference expansion.
- No external reference fetching or arbitrary local-file reference resolution.
- Resolve DNS once and connect to the checked IP. Remote contract loading and
  runtime requests reject private/reserved destinations unless explicitly
  enabled. TLS verifies the original hostname. Redirects are never followed.
  Socket operations use the configured timeout; DNS uses platform resolver
  timing. This is not an absolute end-to-end wall-clock deadline.
- `allow_private` also enables plaintext HTTP; use it only for trusted local or
  internal endpoints. It is disabled by default and is not inferred from a
  contract. No credentials are sent while downloading a contract.
- Credentials remain environment references in configuration. Generated files
  never contain resolved credentials. Built-in diagnostics omit response
  bodies, header values, validation details, and underlying network exceptions.
- Request headers reject control characters. API keys cannot target routing
  headers. No shell command or Python import is accepted from configuration.
- Client policy is a convenience guard. The API server must enforce its own
  authorization. A client cannot prove server permission from a contract.

Use contracts only when you intend to call their declared server. A public
server is still an external destination; inspect contracts and override the base
URL when needed. Custom plugins are trusted executable application code.

Ownership manifests are bounded JSON objects of at most 2 MB with integer schema
version 1 and validated file hashes. Malformed roots, unknown fields, and
excessive input fail before writes. Generation also rejects ownership inventories
that would exceed that bound, so it cannot create an unreadable regeneration
manifest. Literal operation paths reject whitespace and control characters.

Reference resolution limits expanded string keys and values to eight million
characters, counting repeated references each time. This complements the node
and depth budgets and stops small contracts from producing oversized serialized
payloads through repeated large strings. The input document limit remains 2 MB.

First-party `CodaError` subclasses carry nonempty textual diagnostics. Error
classes and the documented CLI exit codes support programmatic handling;
diagnostic wording can evolve.
