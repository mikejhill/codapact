"""L3 shaping contract: explicit, auditable ``Api -> Api`` transforms."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from codapact.core.diagnostics import Diagnostics
from codapact.model.api import Api


@dataclass(slots=True)
class TransformResult:
    """The shaped model plus a JSON-compatible audit trail."""

    api: Api
    report: list[dict[str, Any]] = field(default_factory=list)


class Transform(Protocol):
    """A pure model-to-model step configured from data.

    Transforms never see source documents or targets. Each one validates its
    own configuration and records what it changed in the report.
    """

    name: str

    @abstractmethod
    def apply(
        self, api: Api, config: Mapping[str, Any], diagnostics: Diagnostics
    ) -> TransformResult:
        """Return the transformed model and its audit entries."""
        raise NotImplementedError
