---
type: guide
status: active
authority: informative
criticality: normal
review:
  status: unreviewed
---

# Getting started

## Prerequisites

- Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git.
- Use Python 3.13 or newer; `uv sync` can provision the pinned version.
- Run these commands from the cloned repository root. No API account is needed.

## Generate and inspect

```console
git clone https://github.com/mikejhill/codapact.git
cd codapact
uv sync --locked
uv run codapact --version
uv run codapact generate examples/quickstart/openapi.yaml --output .tmp/quickstart --package demo_pets --slug demo-pets --runtime-allow-private --dry-run
```

The JSON report selects `get_pet`; the output directory does not exist yet.
Generate the package and load its installed command in uv's managed environment:

```console
uv run codapact generate examples/quickstart/openapi.yaml --output .tmp/quickstart --package demo_pets --slug demo-pets --runtime-allow-private
uv run --with . --with ./.tmp/quickstart demo-pets --help
```

Expect `Generated demo_pets ...` and help listing `get-pet`. The local `--with`
package uses the current checkout's matching runtime; no separate manual venv or
package installation is required.

## Make a real local call

1. In a second terminal at the repository root, start the anonymous demo server:

   ```console
   uv run examples/quickstart/server.py
   ```

   It listens only on `127.0.0.1:8765`. Keep that terminal open.

2. In the first terminal, call the generated command:

   ```console
   uv run --with . --with ./.tmp/quickstart demo-pets --base-url http://127.0.0.1:8765 get-pet --arg-pet-id 7
   ```

   Expected JSON: `{"id": 7, "name": "Miso"}`.

3. Press Ctrl+C in the server terminal to stop it. The generated package remains
   in `.tmp/quickstart`; no configuration or credentials are written to your home.

Private/plaintext access is enabled only in this generated demo profile. Use the
default network restrictions for real clients. If port 8765 is occupied, pass
`--port 8766` to the server and use that port in `--base-url`.

## Use your own contract

```console
uv run codapact generate path/to/openapi.yaml --output .tmp/my-client --package my_client --slug my-api
uv run --with . --with ./.tmp/my-client my-api --help
```

- Supported input is the [documented OpenAPI 3.0 JSON/HTTP subset](../reference/supported-features.md).
- Store reusable generation settings in a [manifest](../reference/configuration.md).
- The generated command discovers `$XDG_CONFIG_HOME/my-api/config.json` and
  `auth.json`, falling back to `~/.config/my-api`. Windows uses the same rule.
- Use `--config-dir PATH` to isolate a trial. Missing discovered files are allowed.
- Set credentials through environment references described in
  [runtime configuration](../reference/configuration.md#runtime-discovery-and-precedence).
- If regeneration reports an edited or unowned file, preserve it and choose a new
  output directory. Do not erase user changes to force regeneration.

For a published generator outside a source checkout, run `uv tool install codapact`
and then `codapact generate ...`. Install the generated command with
`uv tool install ./generated-directory`; its runtime version is pinned automatically.
