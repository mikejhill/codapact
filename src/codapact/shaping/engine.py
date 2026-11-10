"""Apply an ordered list of configured transforms, validating after each step."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from codapact.core.diagnostics import Diagnostics
from codapact.core.errors import ConfigError
from codapact.model.api import Api
from codapact.model.validate import validate
from codapact.shaping.base import Transform
from codapact.shaping.transforms import BUILTIN_TRANSFORMS


@dataclass(frozen=True, slots=True)
class ShapeResult:
    """The final model (``None`` on error), audit report, and diagnostics."""

    api: Api | None
    report: list[dict[str, Any]]
    diagnostics: Diagnostics


class Shaper:
    """Run shaping steps of the form ``{transform_name: config}``."""

    def __init__(self, transforms: Iterable[Transform] = BUILTIN_TRANSFORMS) -> None:
        """Register transforms by name; duplicates are rejected."""
        self._transforms: dict[str, Transform] = {}
        for transform in transforms:
            if transform.name in self._transforms:
                raise ConfigError(f"duplicate transform {transform.name!r}")
            self._transforms[transform.name] = transform

    def apply(self, api: Api, steps: Sequence[object]) -> ShapeResult:
        """Apply steps in order; stop at the first step that leaves errors."""
        diagnostics = Diagnostics()
        report: list[dict[str, Any]] = []
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping) or len(step) != 1:
                diagnostics.error("shape.step", f"step {index} must have exactly one key")
                return ShapeResult(None, report, diagnostics)
            ((name, config),) = step.items()
            transform = self._transforms.get(name)
            if transform is None:
                diagnostics.error("shape.unknown", f"unknown transform {name!r}", f"shape[{index}]")
                return ShapeResult(None, report, diagnostics)
            try:
                result = transform.apply(api, config or {}, diagnostics)
            except (ConfigError, TypeError, ValueError, KeyError) as error:
                diagnostics.error("shape.config", str(error), f"shape[{index}].{name}")
                return ShapeResult(None, report, diagnostics)
            diagnostics.extend(validate(result.api))
            report.extend(result.report)
            if diagnostics.has_errors:
                return ShapeResult(None, report, diagnostics)
            api = result.api
        return ShapeResult(api, report, diagnostics)
