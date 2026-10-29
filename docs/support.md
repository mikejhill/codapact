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

## Safety boundaries

- Parse at most 2 MB, reject YAML aliases, custom tags, duplicate keys,
  non-JSON values, excessive depth, and excessive reference expansion.
- No external reference fetching or arbitrary local-file reference resolution.
- Resolve DNS once and connect to the checked IP. Remote contract loading and
  runtime requests reject private/reserved destinations unless explicitly
  enabled. TLS verifies the original hostname. Redirects are never followed.
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
