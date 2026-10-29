"""Data-only generation manifests, explicit overrides, and portable runtime profiles."""

import json
import re
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from codapact.errors import CodaError
from codapact.generation import package_name
from codapact.loading import load, parse
from codapact.net import validate_url
from codapact.values import default_slug, merge, slug_name

GENERATION_SCHEMA = json.loads(
    files("codapact").joinpath("schemas/generation.v1.json").read_text("utf-8")
)
DEFAULTS: dict[str, Any] = {
    "version": 1,
    "contract": {"type": "openapi", "allow_private": False},
    "output": {"directory": "generated", "dry_run": False},
    "client": {"language": "python"},
    "cli": {"style": "argparse", "definition": None},
    "runtime": {"config_directory": None, "defaults": {}},
    "selection": {},
}


def local_path(value: str, base: Path) -> Path:
    if "://" in value:
        raise CodaError("this generation path must be local")
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def runtime_directory(value: str | None) -> str | None:
    """Runtime directories are portable runtime values, never build-relative paths."""
    if value is not None and not (
        Path(value).is_absolute() or value == "~" or value.startswith("~/")
    ):
        raise CodaError("runtime config directory must be absolute or start with ~/")
    if value is not None and any(ord(character) < 32 for character in value):
        raise CodaError("invalid runtime config directory")
    return value


def clean_defaults(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    if result.get("base_url") is None:
        result.pop("base_url", None)
    if result.get("policy", {}).get("allow", object()) is None:
        result["policy"].pop("allow")
    if "base_url" in result:
        validate_url(result["base_url"], allow_query=False)
    return result


def resolve_generation(
    manifest: str | None,
    overrides: dict[str, Any],
    settings: list[str] | None = None,
    *,
    cwd: Path | None = None,
) -> dict[str, Any]:
    base = cwd or Path.cwd()
    document: dict[str, Any] = {}
    if manifest is not None:
        path = local_path(manifest, base).absolute()
        document = load(str(path))
        base = path.parent
        if type(document.get("version")) is not int or document["version"] != 1:
            raise CodaError("generation manifest requires version: 1")
    validator = Draft202012Validator(GENERATION_SCHEMA)
    if not validator.is_valid(document):
        raise CodaError("invalid generation manifest; unknown keys or unsupported values")
    result = merge(merge(DEFAULTS, document), overrides)
    for setting in settings or []:
        key, separator, encoded = setting.partition("=")
        if not separator or not re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)*", key):
            raise CodaError("--set requires a dotted manifest field and JSON value")
        try:
            value = parse(('{"value":' + encoded + "}").encode())["value"]
        except CodaError:
            raise CodaError("--set value must be valid JSON data") from None
        # parse accepts YAML; --set intentionally accepts JSON only.
        try:
            json.loads(encoded)
        except ValueError:
            raise CodaError("--set value must be valid JSON data") from None
        cursor = result
        parts = key.split(".")
        for part in parts[:-1]:
            if not isinstance(cursor.get(part), dict):
                cursor[part] = {}
            cursor = cursor[part]
        cursor[parts[-1]] = value
    if not validator.is_valid(result):
        raise CodaError("invalid generation overrides; unknown keys or unsupported values")
    try:
        source = result["contract"]["source"]
        cli = result["cli"]
        client = result["client"]
        slug = slug_name(cli.get("slug", default_slug(client.get("package", "api"))))
        package = package_name(client.get("package", slug.replace("-", "_") + "_client"))
        if source.startswith(("http://", "https://")):
            validate_url(source)
        elif "://" in source:
            raise CodaError("contract source must be a local path or HTTP(S) URL")
        else:
            source = str(local_path(source, base))
        definition = cli.get("definition")
        definition = load(str(local_path(definition, base))) if definition is not None else None
        directory = runtime_directory(result["runtime"].get("config_directory"))
        defaults = clean_defaults(result["runtime"].get("defaults", {}))
        return {
            "source": source,
            "output": local_path(result["output"]["directory"], base),
            "package": package,
            "source_plugin": result["contract"]["type"],
            "targets": (client["language"], cli["style"]),
            "cli_definition": definition,
            "allow_private": result["contract"]["allow_private"],
            "selection": result.get("selection", {}),
            "dry_run": result["output"].get("dry_run", False),
            "profile": {
                "version": 1,
                "slug": slug,
                "config_directory": directory,
                "defaults": defaults,
            },
        }
    except KeyError:
        raise CodaError(
            "generation requires contract.source and supported output/client/CLI fields"
        ) from None
