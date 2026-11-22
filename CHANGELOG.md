# Changelog

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

## [0.14.0](https://github.com/mikejhill/codapact/compare/v0.13.0...v0.14.0) (2026-11-22)


### Features

* **cli:** profile settings, guided auth login, and passwords asked when needed ([#75](https://github.com/mikejhill/codapact/issues/75)) ([0e9d099](https://github.com/mikejhill/codapact/commit/0e9d099af90960de60c845ebc5c8fe68f69258a7))


### Bug Fixes

* **python:** allow uv_build 0.13 in generated packages ([#73](https://github.com/mikejhill/codapact/issues/73)) ([44b1235](https://github.com/mikejhill/codapact/commit/44b123508c2549c657e3913358548a15ddeceab0))

## [0.13.0](https://github.com/mikejhill/codapact/compare/v0.12.0...v0.13.0) (2026-11-21)


### Features

* call hooks, CLI extensions, browser sign-in, Atlassian contract support ([#71](https://github.com/mikejhill/codapact/issues/71)) ([13e91f4](https://github.com/mikejhill/codapact/commit/13e91f440d7d82e6f11c1d3b338d9ec198416919))

## [0.12.0](https://github.com/mikejhill/codapact/compare/v0.11.0...v0.12.0) (2026-11-20)


### Features

* custom protocol bindings for non-standard APIs ([#69](https://github.com/mikejhill/codapact/issues/69)) ([4085dc7](https://github.com/mikejhill/codapact/commit/4085dc7f46820cbaf1884192425a499204f269f4))

## [0.11.0](https://github.com/mikejhill/codapact/compare/v0.10.0...v0.11.0) (2026-11-20)


### Features

* generate coda READMEs in one common format ([#68](https://github.com/mikejhill/codapact/issues/68)) ([538354f](https://github.com/mikejhill/codapact/commit/538354f9cc7e4cbf8b9d8565dfc960bee1f9283c))


### Documentation

* codify codapact-&lt;api&gt; naming policy for self-published codas ([#66](https://github.com/mikejhill/codapact/issues/66)) ([ff01639](https://github.com/mikejhill/codapact/commit/ff01639caa59212958518ff2ff4458d0b759d264))

## [0.10.0](https://github.com/mikejhill/codapact/compare/v0.9.1...v0.10.0) (2026-11-19)


### Features

* **cli:** compact JSON output when stdout is not a terminal ([#62](https://github.com/mikejhill/codapact/issues/62)) ([1b80666](https://github.com/mikejhill/codapact/commit/1b80666128834f5ba2a13d5e21e301e6f60fbafd))
* **cli:** leaner agent discovery (lean describe COMMAND, compact JSON, grouped --brief, JSON field hints) ([#61](https://github.com/mikejhill/codapact/issues/61)) ([4070dea](https://github.com/mikejhill/codapact/commit/4070deab7eb77dabeb65cda52228af6ab8ac7232))
* generate large real-world contracts strictly; session checks and expiry hooks ([#58](https://github.com/mikejhill/codapact/issues/58)) ([523e6b3](https://github.com/mikejhill/codapact/commit/523e6b3ac156fb7f6516d157c4b1f7809519ebee))
* headers shaping step; multipart lists of files ([#60](https://github.com/mikejhill/codapact/issues/60)) ([7bbb762](https://github.com/mikejhill/codapact/commit/7bbb762a97aba656803d08ade48111747647a8b5))
* move the agent plugin to extras/agent-plugin; dispatch CodeQL for release PRs ([#65](https://github.com/mikejhill/codapact/issues/65)) ([3e6c2f1](https://github.com/mikejhill/codapact/commit/3e6c2f14b69a1e86f92d0a4cc19f104f89a99d5d))
* move the agent plugin to plugins/codapact and add live examples ([#64](https://github.com/mikejhill/codapact/issues/64)) ([98c889d](https://github.com/mikejhill/codapact/commit/98c889d6ad8e03cbe08f1b69ea05288f16fcd855))


### Documentation

* **discovery:** remeasure on main after [#61](https://github.com/mikejhill/codapact/issues/61) and [#62](https://github.com/mikejhill/codapact/issues/62) ([#63](https://github.com/mikejhill/codapact/issues/63)) ([7e9cdbb](https://github.com/mikejhill/codapact/commit/7e9cdbb5e2b0fde3d39e27c66c21abb116ed2329))
* measure how efficiently agents discover a generated CLI ([#57](https://github.com/mikejhill/codapact/issues/57)) ([1be40c8](https://github.com/mikejhill/codapact/commit/1be40c8daae2a80c4b9a404c27865c1616571b1f))

## [0.9.1](https://github.com/mikejhill/codapact/compare/v0.9.0...v0.9.1) (2026-11-15)


### Bug Fixes

* **cli:** plain usage lines in describe; uvx-only coda skills; Agent Plugins manifest ([#54](https://github.com/mikejhill/codapact/issues/54)) ([3fea349](https://github.com/mikejhill/codapact/commit/3fea3493542eff6860cf2db049bd8cecce67d52b))

## [0.9.0](https://github.com/mikejhill/codapact/compare/v0.8.0...v0.9.0) (2026-11-15)


### Features

* **cli:** agent start-up path in describe; create-coda-skill skill ([#51](https://github.com/mikejhill/codapact/issues/51)) ([aecf023](https://github.com/mikejhill/codapact/commit/aecf02313d171ce644619ea6fa3a6b45cc38b040))
* **cli:** name the command after its distribution; carry logins across renames ([#52](https://github.com/mikejhill/codapact/issues/52)) ([019c6d8](https://github.com/mikejhill/codapact/commit/019c6d8b395032d7a0b3946df790d1253cbade61))

## [0.8.0](https://github.com/mikejhill/codapact/compare/v0.7.0...v0.8.0) (2026-11-14)


### Features

* **cli:** report the installed distribution's version ([#49](https://github.com/mikejhill/codapact/issues/49)) ([d7d663d](https://github.com/mikejhill/codapact/commit/d7d663d611548534e3657b1ffc6ec8a048ebc96d))

## [0.7.0](https://github.com/mikejhill/codapact/compare/v0.6.1...v0.7.0) (2026-11-14)


### Features

* **auth:** credential profiles; doctor names the exact keyring entry ([#47](https://github.com/mikejhill/codapact/issues/47)) ([de86a9e](https://github.com/mikejhill/codapact/commit/de86a9ec0eefd7cc9e4b65ba76abb8ee99088fcd))

## [0.6.1](https://github.com/mikejhill/codapact/compare/v0.6.0...v0.6.1) (2026-11-14)


### Bug Fixes

* **auth:** namespace keyring entries as codapact:PROGRAM:SCHEME ([#45](https://github.com/mikejhill/codapact/issues/45)) ([8c46322](https://github.com/mikejhill/codapact/commit/8c463221052a017e2a5626e888f76eb26bc84c43))

## [0.6.0](https://github.com/mikejhill/codapact/compare/v0.5.2...v0.6.0) (2026-11-14)


### Features

* **auth:** credential fields for schemes that need several values ([#43](https://github.com/mikejhill/codapact/issues/43)) ([9472a7f](https://github.com/mikejhill/codapact/commit/9472a7f1ba351ad06e2b65b038b4b0fa8e45b9f3))
* **cli:** cli.env_prefix names environment variables independently of the program ([#42](https://github.com/mikejhill/codapact/issues/42)) ([f7b5041](https://github.com/mikejhill/codapact/commit/f7b504196c478d266ab0f8a3bd2ee8aea9661991))
* codapact sync and generation metadata in manifests ([#41](https://github.com/mikejhill/codapact/issues/41)) ([fc2a27c](https://github.com/mikejhill/codapact/commit/fc2a27c518790cde6f06cf95a95dd582622a6ffd))

## [0.5.2](https://github.com/mikejhill/codapact/compare/v0.5.1...v0.5.2) (2026-11-14)


### Bug Fixes

* **cli:** fail when --query matches nothing ([#39](https://github.com/mikejhill/codapact/issues/39)) ([08bd3aa](https://github.com/mikejhill/codapact/commit/08bd3aa5a3e45192461633c084feb7d9438cb193))

## [0.5.1](https://github.com/mikejhill/codapact/compare/v0.5.0...v0.5.1) (2026-11-14)


### Bug Fixes

* **cli:** configurable response limit, UTF-8 pipes, jsonl records ([#37](https://github.com/mikejhill/codapact/issues/37)) ([f8a3b72](https://github.com/mikejhill/codapact/commit/f8a3b72ac2d95f2d873cfa65db2104a4750b029a))

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
