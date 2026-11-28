"""Orchestrate the layers. Every stage is a separate method with explicit inputs.

```
acquire → translate → validate → shape → surface → commands → generate → write
   L0        L1          L2        L3       L4         L5        L4/L6     L7
```
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codapact import __version__
from codapact.commands.derive import derive_commands
from codapact.commands.model import CommandTree
from codapact.core.diagnostics import Diagnostics, Severity
from codapact.core.errors import ConfigError, PluginError, expect
from codapact.model.api import Api
from codapact.model.validate import validate
from codapact.pipeline import writer
from codapact.pipeline.config import ProjectConfig, TargetConfig
from codapact.plugins.external import ExternalTarget
from codapact.plugins.protocol import GeneratedFile, GenerationRequest, Target
from codapact.plugins.registry import Registry
from codapact.shaping.engine import Shaper
from codapact.shaping.transforms import selection
from codapact.sources.base import SourceInput
from codapact.sources.loader import Fetcher
from codapact.surface.derive import SurfaceOptions, derive_surface
from codapact.surface.model import Surface


@dataclass(slots=True)
class PipelineResult:
    """Every intermediate artifact, for inspection and testing."""

    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    api: Api | None = None
    surface: Surface | None = None
    commands: CommandTree | None = None
    shape_report: list[dict[str, Any]] = field(default_factory=list)
    outputs: dict[Path, tuple[GeneratedFile, ...]] = field(default_factory=dict)
    plans: dict[Path, writer.WritePlan] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    """What was generated from: contract location, upstream and SHA-256 digests."""


class Pipeline:
    """Run a ``ProjectConfig`` through the registered plugins."""

    def __init__(self, registry: Registry, fetcher: Fetcher | None = None) -> None:
        """Bind plugins and the contract fetcher."""
        self.registry = registry
        self.fetcher = fetcher or Fetcher()

    def model(self, config: ProjectConfig, result: PipelineResult) -> Api | None:
        """L0-L3: read, translate, validate, and shape the contract."""
        source = self.registry.source(config.source.type)
        limit = config.source.max_bytes
        document = self.fetcher.fetch(config.source.location, base=config.base, max_bytes=limit)
        companions = tuple(
            self.fetcher.fetch(d, base=config.base, max_bytes=limit)
            for d in config.source.documents
        )
        result.source = source_metadata(config, document.content, [c.content for c in companions])
        keep = selection(config.shape[0]["select"]) if _selects_first(config.shape) else None
        translated = source.parse(
            SourceInput(document, companions, config.source.options, keep=keep)
        )
        result.diagnostics.extend(translated.diagnostics)
        if translated.api is None:
            return None
        structural = validate(translated.api)
        deferred = structural.has_errors and _selects_first(config.shape)
        if structural.has_errors and not deferred:
            result.diagnostics.extend(structural)
            return None
        if not deferred:
            result.diagnostics.extend(structural)
        shaped = Shaper(self.registry.transforms.values()).apply(translated.api, config.shape)
        if deferred and shaped.api is not None:
            # The leading ``select`` decides what is generated: problems it removed do not
            # block generation, and problems it kept are re-reported by the shaper.
            count = len([d for d in structural if d.severity is Severity.ERROR])
            result.diagnostics.warning(
                "model.outside-selection",
                f"{count} model error(s) in parts removed by select were ignored",
            )
        result.diagnostics.extend(shaped.diagnostics)
        result.shape_report = shaped.report
        result.api = shaped.api
        return shaped.api

    def describe(self, config: ProjectConfig, result: PipelineResult) -> None:
        """L4/L5 descriptors: library surface and command tree."""
        api = expect(result.api, "model")
        result.surface = derive_surface(
            api, SurfaceOptions.from_config(config.surface), result.diagnostics
        )
        program = config.cli.get("program") or _distribution_program(config)
        prefix = config.cli.get("env_prefix")
        result.commands = derive_commands(
            api,
            result.surface,
            program=str(program) if program else None,
            env_prefix=str(prefix) if prefix else None,
            diagnostics=result.diagnostics,
        )

    def target(self, target: TargetConfig, base: Path) -> Target:
        """Resolve a configured target; relative plugin paths resolve against ``base``."""
        if target.plugin is not None:
            head, *rest = target.plugin
            if "/" in head and not Path(head).is_absolute():
                head = str(base / head)
            return ExternalTarget.describe((head, *rest))
        return self.registry.target(expect(target.name, "target name"))

    def generate(self, config: ProjectConfig, result: PipelineResult) -> None:
        """Invoke every target and collect files by output directory."""
        api = expect(result.api, "model")
        surface = expect(result.surface, "surface")
        shared = _shared_options(config)
        for target_config in config.targets:
            target = self.target(target_config, config.base)
            self.registry.check(target, api)
            options = {**shared.get(target_config.output, {}), **target_config.options}
            if "include" in options:
                options["include_files"] = _read_includes(options["include"], config.base)
            package_output = _package_output(config, target_config)
            if package_output is not None:
                if target.name == "python-readme":
                    for key, value in shared.get(package_output, {}).items():
                        options.setdefault(key, value)
                if any(
                    t.name == "python-cli" and t.output == package_output for t in config.targets
                ):
                    options.setdefault(
                        "command", result.commands.program.kebab if result.commands else None
                    )
                    options.setdefault(
                        "env_prefix", result.commands.env_prefix if result.commands else None
                    )
                options.setdefault(
                    "readme", _readme_context(config, result, target_config.output, package_output)
                )
            if target.needs_commands:
                for key in ("auth_plugin", "auth", "previous_programs", "extensions"):
                    if key in config.cli:
                        options.setdefault(key, config.cli[key])
                if _skill_enabled(config):
                    options.setdefault("skill", _skill_option(config))
                if config.e2e:
                    options.setdefault("e2e", dict(config.e2e))
            request = GenerationRequest(
                api=api,
                surface=surface,
                commands=result.commands if target.needs_commands else None,
                options=options,
                generator_version=__version__,
            )
            response = target.generate(request)
            result.diagnostics.extend(response.diagnostics)
            existing = result.outputs.get(target_config.output, ())
            clash = {f.path for f in existing} & {f.path for f in response.files}
            if clash:
                raise PluginError(f"targets write the same files: {sorted(clash)}")
            result.outputs[target_config.output] = existing + response.files

    def run(self, config: ProjectConfig, *, dry_run: bool = False) -> PipelineResult:
        """Run every stage; write files unless ``dry_run``."""
        result = PipelineResult()
        if self.model(config, result) is None or result.diagnostics.has_errors:
            return result
        self.describe(config, result)
        if result.diagnostics.has_errors:
            return result
        self.generate(config, result)
        if result.diagnostics.has_errors:
            return result
        for root, files in result.outputs.items():
            result.plans[root] = writer.plan(root, files)
        if not dry_run:
            for plan in result.plans.values():
                metadata: dict[str, Any] = {"source": result.source}
                if config.digest:
                    metadata["config_sha256"] = config.digest
                writer.write(plan, generator=f"codapact/{__version__}", metadata=metadata)
        return result


def source_metadata(
    config: ProjectConfig, content: bytes, companions: list[bytes]
) -> dict[str, Any]:
    """Describe the contract an output was generated from, for the manifest."""
    metadata: dict[str, Any] = {
        "type": config.source.type,
        "location": config.source.location,
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    if config.source.upstream:
        metadata["upstream"] = config.source.upstream
    if config.source.documents:
        metadata["documents"] = {
            name: hashlib.sha256(data).hexdigest()
            for name, data in zip(config.source.documents, companions, strict=True)
        }
    return metadata


def _package_output(config: ProjectConfig, target: TargetConfig) -> Path | None:
    """The Python package a ``python`` or ``python-readme`` target documents."""
    if target.name == "python":
        return target.output
    if target.name == "python-readme":
        return next((t.output for t in config.targets if t.name == "python"), None)
    return None


def _relative(path: Path, start: Path) -> str:
    """``path`` relative to ``start``, or absolute when they are on different drives."""
    try:
        return Path(os.path.relpath(path.absolute(), start.absolute())).as_posix()
    except ValueError:  # Windows: no relative path between drives
        return path.absolute().as_posix()


def _readme_context(
    config: ProjectConfig, result: PipelineResult, output: Path, package: Path
) -> dict[str, Any]:
    """What a README needs beyond the model: settings, the pinned source, credential docs."""
    location = config.source.location
    source: dict[str, Any] = {
        "type": config.source.type,
        "location": location,
        "sha256": result.source.get("sha256", ""),
        "upstream": config.source.upstream,
        "contract": None,
        "contract_path": None,
    }
    if not location.startswith(("https://", "http://")):
        contract = config.base / location
        source["contract"] = _relative(contract, output)
        source["contract_path"] = _relative(contract, config.base)
    auth = config.cli.get("auth") or {}
    docs = {
        str(name): str(spec["docs"])
        for name, spec in auth.items()
        if isinstance(spec, dict) and isinstance(spec.get("docs"), str)
    }
    fields = {
        str(name): [str(f) for f in spec["fields"]]
        for name, spec in auth.items()
        if isinstance(spec, dict) and isinstance(spec.get("fields"), dict)
    }
    return {
        "settings": dict(config.readme),
        "source": source,
        "auth_docs": docs,
        "auth_fields": fields,
        "package_dir": _relative(package, output),
        "skill": _skill_enabled(config),
    }


SKILL_KEYS = frozenset({"description", "api_name", "path"})


def _skill_enabled(config: ProjectConfig) -> bool:
    """``cli.skill`` is set to anything but ``false``/``null`` (``{}`` enables it too)."""
    return config.cli.get("skill") not in (None, False)


def _skill_option(config: ProjectConfig) -> dict[str, Any]:
    """Resolve ``cli.skill`` for the python-cli target.

    ``true`` or ``{description, api_name}`` selects the standard thin skill;
    ``{path}`` names a hand-written ``SKILL.md`` or a folder holding one (relative to the
    project file), copied as is.
    """
    raw = config.cli["skill"]
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("cli.skill must be true or a mapping (description, api_name, or path)")
    unknown = set(raw) - SKILL_KEYS
    if unknown:
        raise ConfigError(f"cli.skill: unknown keys {sorted(unknown)}")
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"cli.skill.{key} must be a non-empty string")
    if "path" not in raw:
        return {"enabled": True, **raw}
    if set(raw) != {"path"}:
        raise ConfigError("cli.skill.path cannot be combined with description or api_name")
    root = config.base.resolve()
    path = (config.base / raw["path"]).resolve()
    if not path.is_relative_to(root):
        raise ConfigError(f"cli.skill.path {raw['path']!r} is outside the project directory")
    if path.is_file():
        sources = {"SKILL.md": path}
    elif path.is_dir():
        sources = {
            p.relative_to(path).as_posix(): p
            for p in sorted(path.rglob("*"))
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(path).parts)
        }
        for rel, source in sources.items():  # a symlink must not reach outside the project
            if not source.resolve().is_relative_to(root):
                raise ConfigError(f"cli.skill.path: {rel} links outside the project directory")
    else:
        raise ConfigError(f"cli.skill.path {raw['path']!r} does not exist")
    if "SKILL.md" not in sources:
        raise ConfigError(f"cli.skill.path {raw['path']!r} has no SKILL.md")
    files: dict[str, str] = {}
    for rel, source in sources.items():
        try:
            files[rel] = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(
                f"cli.skill.path: cannot read {rel} (skills can only ship UTF-8 text files): {exc}"
            ) from None
    return {"files": files}


def _read_includes(include: Any, base: Path) -> dict[str, str]:
    """Read hand-written files a target copies into its output (``include`` option).

    ``include`` maps an output path (relative to the target's package or output root)
    to a source file relative to the project file; sources outside the project
    directory are refused.
    """
    if not isinstance(include, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in include.items()
    ):
        raise ConfigError("target option 'include' must map output paths to source files")
    root = base.resolve()
    files: dict[str, str] = {}
    for output, source in include.items():
        path = (base / source).resolve()
        if not path.is_relative_to(root):
            raise ConfigError(f"include {source!r} is outside the project directory")
        try:
            files[output] = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"include {source!r}: {exc.strerror or exc}") from None
        except UnicodeDecodeError:
            raise ConfigError(f"include {source!r} is not UTF-8 text") from None
    return files


def _selects_first(shape: tuple[Any, ...]) -> bool:
    """Return whether shaping starts with ``select``, which may drop invalid parts."""
    return bool(shape) and isinstance(shape[0], dict) and set(shape[0]) == {"select"}


def _distribution_program(config: ProjectConfig) -> str | None:
    """Default program name: the distribution of the Python package that ships the CLI.

    A CLI installed as ``acme-api`` is then also run as ``acme-api``, so
    ``uvx acme-api`` works without ``--from``. Falls back to the API name when no
    Python target names a distribution or package.
    """
    outputs = {t.output for t in config.targets if t.name == "python-cli"}
    shared = _shared_options(config)
    for output in outputs:
        options = shared.get(output, {})
        name = options.get("distribution") or options.get("package")
        if name:
            return str(name).replace("_", "-")
    return None


def _shared_options(config: ProjectConfig) -> dict[Path, dict[str, Any]]:
    """Targets sharing an output directory share options (e.g. the package name)."""
    shared: dict[Path, dict[str, Any]] = {}
    for target in config.targets:
        merged = shared.setdefault(target.output, {})
        for key in ("package", "distribution", "version", "protocols"):
            if key in target.options:
                merged.setdefault(key, target.options[key])
    return shared
