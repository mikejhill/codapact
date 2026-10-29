# CLI and runtime configuration

The version 1 CLI definition assigns stable command names to operations:

```json
{
  "version": 1,
  "commands": [{"name": "show-pet", "operation": "get_pet"}]
}
```

Supply `--cli-definition path.json` during generation. Every operation must be
covered, names must be unique, and command names use lowercase letters, digits,
and hyphens. Without a definition, underscores in operation IDs become hyphens.
Flags use `--arg-<parameter-name>` to avoid collisions with CLI options. Required
parameters become required flags. Non-string values are JSON scalars. Bodies
use `--body '{"id":1,"name":"Miso"}'`. Global options precede the command.

The editor schema is [CLI definition v1](../schemas/cli.v1.schema.json). Generation
also checks command uniqueness, operation coverage, and flag collisions against
the actual contract.

Runtime configuration is local JSON/YAML, supplied with `--config path.json`:

```json
{
  "version": 1,
  "base_url": "https://api.example.invalid",
  "timeout": 15,
  "allow_private": false,
  "auth": {"token": {"env": "PETS_TOKEN"}},
  "policy": {"allow": ["get_pet"], "deny": ["delete_pet"]}
}
```

Auth keys match the contract's security scheme names. Basic auth uses
`username_env` and `password_env`. No literal credential values are supported.
An omitted allow list allows all operations except the deny list; an empty allow
list denies all. Deny always wins. Credentials resolve when a call executes.

The CLI never prompts. Successful output is JSON on stdout, including `null`
for empty responses; `--pretty` indents output. Errors go to stderr.

| Exit | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Arguments, configuration, request validation, or generation failure |
| 3 | Authentication unavailable or client policy denied |
| 4 | Network, TLS, redirect, or transport limit failure |
| 5 | HTTP API failure |
| 6 | Response violates declared contract |
