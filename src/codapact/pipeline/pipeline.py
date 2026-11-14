"""Orchestrate the layers. Every stage is a separate method with explicit inputs.

```
acquire → translate → validate → shape → surface → commands → generate → write
   L0        L1          L2        L3       L4         L5        L4/L6     L7
```
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codapact import __version__
from codapact.commands.derive import derive_commands
from codapact.commands.model import CommandTree
from codapact.core.diagnostics import Diagnostics, Severity
from codapact.core.errors import PluginError, expect
from codapact.model.api import Api
from codapact.model.validate import validate
from codapact.pipeline import writer
from codapact.pipeline.config import ProjectConfig, TargetConfig
from codapact.plugins.external import ExternalTarget
from codapact.plugins.protocol import GeneratedFile, GenerationRequest, Target
from codapact.plugins.registry import Registry
from codapact.shaping.engine import Shaper
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
        translated = source.parse(SourceInput(document, companions, config.source.options))
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
        program = config.cli.get("program")
        result.commands = derive_commands(
            api,
            result.surface,
            program=str(program) if program else None,
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
            if target.name == "python" and any(
                t.name == "python-cli" and t.output == target_config.output for t in config.targets
            ):
                options.setdefault(
                    "command", result.commands.program.kebab if result.commands else None
                )
            if target.needs_commands:
                for key in ("auth_plugin", "auth"):
                    if key in config.cli:
                        options.setdefault(key, config.cli[key])
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


def _selects_first(shape: tuple[Any, ...]) -> bool:
    """Return whether shaping starts with ``select``, which may drop invalid parts."""
    return bool(shape) and isinstance(shape[0], dict) and set(shape[0]) == {"select"}


def _shared_options(config: ProjectConfig) -> dict[Path, dict[str, Any]]:
    """Targets sharing an output directory share options (e.g. the package name)."""
    shared: dict[Path, dict[str, Any]] = {}
    for target in config.targets:
        merged = shared.setdefault(target.output, {})
        for key in ("package", "distribution", "version"):
            if key in target.options:
                merged.setdefault(key, target.options[key])
    return shared
