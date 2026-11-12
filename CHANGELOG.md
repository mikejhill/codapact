# Changelog

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

## [0.5.0](https://github.com/mikejhill/codapact/compare/v0.4.0...v0.5.0) (2026-11-12)


### Features

* **cli:** pick unwraps envelopes; keyring for generated CLIs ([#32](https://github.com/mikejhill/codapact/issues/32)) ([f1c5104](https://github.com/mikejhill/codapact/commit/f1c51040a9bd5c21aa3e634955f2b5c0b7a4a6a1))


### Bug Fixes

* **e2e:** fill a required body whose fields are all optional in dry-run cases ([#31](https://github.com/mikejhill/codapact/issues/31)) ([c9a4ebb](https://github.com/mikejhill/codapact/commit/c9a4ebbad4338443689c0bf732c4b1e4cb266826))

## [0.4.0](https://github.com/mikejhill/codapact/compare/v0.3.0...v0.4.0) (2026-11-12)


### Features

* **auth:** pluggable auth (OAuth 2.0 flows, bespoke authenticators) and generated bundle e2e suites ([#27](https://github.com/mikejhill/codapact/issues/27)) ([b42cb61](https://github.com/mikejhill/codapact/commit/b42cb61165c58895893c1a50a5515d5dc39a88aa))
* **cli:** conventional help, version, describe, doctor, completion, dry-run, JSON errors ([#25](https://github.com/mikejhill/codapact/issues/25)) ([809c9da](https://github.com/mikejhill/codapact/commit/809c9da83102ebaa5ae41a0cd0d60177e10c286f))
* **cli:** name commands in errors, describe --brief and schemas, CLI conformance suite ([#26](https://github.com/mikejhill/codapact/issues/26)) ([8780dfc](https://github.com/mikejhill/codapact/commit/8780dfcf1607f267cf59c9d39e8dd4c61a895da7))
* fix YNAB-evaluation findings (nullability, allOf, naming, CLI usability) ([#22](https://github.com/mikejhill/codapact/issues/22)) ([d8caaf3](https://github.com/mikejhill/codapact/commit/d8caaf33d084770993a5ad45d7bfb3d01d5d7cf9))


### Bug Fixes

* address defects found by generating clients for PokeAPI and GitHub ([#23](https://github.com/mikejhill/codapact/issues/23)) ([b0693c3](https://github.com/mikejhill/codapact/commit/b0693c370d0fbfc6483cdc82adc4015c375a05a1))

## [0.3.0](https://github.com/mikejhill/codapact/compare/v0.2.0...v0.3.0) (2026-11-10)


### ⚠ BREAKING CHANGES

* the CLI, configuration format, and generated code are new.

### Features

* rewrite codapact as a layered contract-to-library-and-CLI generator ([#20](https://github.com/mikejhill/codapact/issues/20)) ([594ae46](https://github.com/mikejhill/codapact/commit/594ae46258b64d478e33313c9ca93033cbfff226))

## [0.2.0](https://github.com/mikejhill/codapact/compare/v0.1.1...v0.2.0) (2026-10-29)

### Features

* Generate the same specialized endpoint subset for typed clients and CLI wrappers, using
  operation IDs, method names, path globs, and domain tags with explicit include/exclude rules.
* Add versioned generation settings, CLI overrides, an owned selection report, and dry-run
  generation/ownership preflight without output writes.
* Preserve selected nested schema dependencies and authentication requirements, stable CLI
  renames, deterministic regeneration, and unrelated user files.
* Provide a GET/HEAD/OPTIONS read preset and runnable domain profiles. Method selection is
  not a safety guarantee or server authorization control.

Implemented in [PR #16](https://github.com/mikejhill/codapact/pull/16).

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
