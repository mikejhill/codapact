# CodaPact

![CodaPact wordmark](branding/wordmark.svg)

**Contracts into callable tools.**

This local first iteration generates an installable, typed Python client and a
scriptable CLI from a local or remote OpenAPI contract. The package, module, and
generator CLI are named `codapact`. Package registry publication is pending.

```powershell
uv sync
uv run codapact generate examples/pets.yaml --output .tmp/pets-client --package pets_client
uv build
uv pip install --no-deps .tmp/pets-client
uv run pets-client --config examples/runtime.json get-pet --arg-pet-id 7
```

The example server address is intentionally inert. Override `base_url` with your
own trusted endpoint and set the environment variable referenced by the runtime
configuration. Tests use isolated loopback HTTP servers and synthetic credentials.

```python
from pets_client import Client

client = Client(settings={"auth": {"token": {"env": "PETS_TOKEN"}}})
pet = client.get_pet(parameters={"pet_id": 7})
```

- [Architecture and extension contracts](docs/architecture.md)
- [Supported features and safety boundaries](docs/support.md)
- [CLI definition and runtime configuration](docs/configuration.md)
- [Release process](docs/releasing.md)
- [Verification evidence and limits](docs/verification.md)
- [Asynchronous mutation testing and open alerts](docs/mutation.md)

Run `uv run poe check` for formatting, lint, type checking, and tests. Generated
packages depend on this runtime at the exact generator version. Before public
publication, install the local runtime wheel and generated wheel together. The
end-to-end test proves that workflow in a fresh environment.
