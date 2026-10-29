# Changelog

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

## [0.1.1](https://github.com/mikejhill/codapact/compare/v0.1.0...v0.1.1) (2026-10-29)

### Bug Fixes

* Reject whitespace and DEL in literal OpenAPI operation paths during translation.
* Validate and bound ownership manifests before regeneration writes, including malformed roots,
  non-integer versions, and oversized inventories.
* Honor explicit base URL, timeout, and private-network flags in wrappers without discovery profiles;
  preserve configured values when flags are omitted.
* Reject all ASCII control characters and DEL in parameter and custom-auth request headers before
  transport, with redacted diagnostics.

* Bound expanded reference text, counting repeated large string keys and values before serialization.

The first four fixes are included in [3e21fd7](https://github.com/mikejhill/codapact/commit/3e21fd7b42dc370e27bfa7617c6ed9174d2cb284).

## 0.1.0 (2026-10-29)

### Features

* add generation manifests and XDG runtime profiles ([cd0e6a2](https://github.com/mikejhill/codapact/commit/cd0e6a20e813423d3b33ca5ba277a036c482dd8c))
* initialize codapact ([7daa7e2](https://github.com/mikejhill/codapact/commit/7daa7e2534bdd59bcd555dc90ec2e97a002f130c))

### Bug Fixes

* document generated commands and release handoff ([9ea472c](https://github.com/mikejhill/codapact/commit/9ea472ca5e3cfe9fdf4bbc94bb22511d87eece22))
* harden contracts and add mutation alerts ([b4b1a06](https://github.com/mikejhill/codapact/commit/b4b1a06d2029a53fa68783f6ecd1c0dedb7854e2))
* reconcile asynchronous mutation alerts ([ba1737d](https://github.com/mikejhill/codapact/commit/ba1737d33f9e6157a7d9fe6f6c8b1d2878917dec))
