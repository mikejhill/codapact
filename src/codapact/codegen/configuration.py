"""Data-only generation manifests, explicit overrides, and portable runtime profiles."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator

from codapact.codegen.emitters import ArtifactNames
from codapact.contracts.data import GenerationManifest, GenerationOptions, RuntimeSettings
from codapact.exceptions import CodaError
from codapact.infrastructure.documents import DocumentLoader
from codapact.infrastructure.http import UrlValidator
from codapact.infrastructure.validation import SchemaValidator
from codapact.infrastructure.values import JsonValues, Names


class GenerationConfiguration:
    """Generation configuration service."""

    @staticmethod
    def defaults() -> dict[str, object]:
        """Return independent built-in manifest defaults."""
        return {
            "version": 1,
            "contract": {"type": "openapi", "allow_private": False},
            "output": {"directory": "generated", "dry_run": False},
            "client": {"language": "python"},
            "cli": {"style": "argparse", "definition": None},
            "runtime": {"config_directory": None, "defaults": {}},
            "selection": {},
        }

    @staticmethod
    def local_path(value: str, base: Path) -> Path:
        """Resolve a local generation path against its declared base."""
        if "://" in value:
            raise CodaError("this generation path must be local")
        path = Path(value).expanduser()
        return path if path.is_absolute() else base / path

    @staticmethod
    def runtime_directory(value: str | None) -> str | None:
        """Runtime directories are portable runtime values, never build-relative paths."""
        if value is not None and not (
            Path(value).is_absolute() or value == "~" or value.startswith("~/")
        ):
            raise CodaError("runtime config directory must be absolute or start with ~/")
        if value is not None and any(ord(character) < 32 for character in value):
            raise CodaError("invalid runtime config directory")
        return value

    @staticmethod
    def clean_defaults(value: Mapping[str, object]) -> RuntimeSettings:
        """Remove nullable overrides and validate the remaining ordinary settings."""
        result = deepcopy(dict(value))
        if result.get("base_url") is None:
            result.pop("base_url", None)
        policy = result.get("policy", {})
        if isinstance(policy, dict) and policy.get("allow", object()) is None:
            policy.pop("allow")
        settings = SchemaValidator.config(result)
        if "base_url" in settings:
            UrlValidator.validate_url(settings["base_url"], allow_query=False)
        return settings

    @staticmethod
    def resolve_generation(
        manifest: str | None,
        overrides: Mapping[str, object],
        settings: Sequence[str] | None = None,
        *,
        cwd: Path | None = None,
    ) -> GenerationOptions:
        """Validate a manifest and resolve deterministic generation overrides."""
        base = cwd or Path.cwd()
        document: dict[str, object] = {}
        if manifest is not None:
            path = GenerationConfiguration.local_path(manifest, base).absolute()
            document = DocumentLoader.load(str(path))
            base = path.parent
            if type(document.get("version")) is not int or document["version"] != 1:
                raise CodaError("generation manifest requires version: 1")
        validator = Draft202012Validator(
            json.loads(files("codapact").joinpath("schemas/generation.v1.json").read_text("utf-8"))
        )
        if not validator.is_valid(document):
            raise CodaError("invalid generation manifest; unknown keys or unsupported values")
        result = JsonValues.merge(
            JsonValues.merge(GenerationConfiguration.defaults(), document), overrides
        )
        for setting in settings or []:
            key, separator, encoded = setting.partition("=")
            if not separator or not re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)*", key):
                raise CodaError("--set requires a dotted manifest field and JSON value")
            try:
                value = DocumentLoader.parse(('{"value":' + encoded + "}").encode())["value"]
            except CodaError:
                raise CodaError("--set value must be valid JSON data") from None
            # parse accepts YAML; --set intentionally accepts JSON only.
            try:
                json.loads(encoded)
            except ValueError:
                raise CodaError("--set value must be valid JSON data") from None
            JsonValues.assign(result, key, value)
        if not validator.is_valid(result):
            raise CodaError("invalid generation overrides; unknown keys or unsupported values")
        # The packaged schema establishes nested field types; missing source fails below.
        values = cast(GenerationManifest, result)
        try:
            source = values["contract"]["source"]
            cli = values["cli"]
            client = values["client"]
            slug = Names.slug_name(
                cli.get("slug", Names.default_slug(client.get("package", "api")))
            )
            package = ArtifactNames.package_name(
                client.get("package", slug.replace("-", "_") + "_client")
            )
            if source.startswith(("http://", "https://")):
                UrlValidator.validate_url(source)
            elif "://" in source:
                raise CodaError("contract source must be a local path or HTTP(S) URL")
            else:
                source = str(GenerationConfiguration.local_path(source, base))
            definition = cli.get("definition")
            cli_definition = (
                DocumentLoader.load(str(GenerationConfiguration.local_path(definition, base)))
                if definition is not None
                else None
            )
            directory = GenerationConfiguration.runtime_directory(
                values["runtime"].get("config_directory")
            )
            defaults = GenerationConfiguration.clean_defaults(values["runtime"].get("defaults", {}))
            return {
                "source": source,
                "output": GenerationConfiguration.local_path(values["output"]["directory"], base),
                "package": package,
                "source_plugin": values["contract"]["type"],
                "targets": (client["language"], cli["style"]),
                "cli_definition": cli_definition,
                "allow_private": values["contract"]["allow_private"],
                "selection": values.get("selection", {}),
                "dry_run": values["output"].get("dry_run", False),
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
