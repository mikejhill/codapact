"""Versioned source-neutral intermediate representation and plugin contracts."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Protocol, cast

from codapact.contracts.data import ContractData, JsonSchema, ParameterData, SecurityScheme
from codapact.exceptions import CodaError

API_VERSION = 1
IR_VERSION = 1


@dataclass(frozen=True, slots=True)
class Operation:
    """Immutable normalized API operation and its schema contracts."""

    id: str
    method: str
    path: str
    parameters: tuple[ParameterData, ...]
    body: JsonSchema | None
    body_required: bool
    responses: dict[str, JsonSchema | None]
    security: tuple[dict[str, list[str]], ...]
    summary: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Contract:
    """Immutable source-neutral API contract and extension metadata."""

    title: str
    base_url: str
    operations: tuple[Operation, ...]
    security_schemes: dict[str, SecurityScheme]
    types: dict[str, JsonSchema] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    version: int = IR_VERSION

    def to_dict(self) -> ContractData:
        """Serialize the typed IR without changing its field names."""
        # Dataclass fields and nested operation records have the ContractData shape.
        return cast(ContractData, asdict(self))


class Translator(Protocol):
    """Protocol for versioned contract translation plugins."""

    api_version: int
    capabilities: frozenset[str]

    @abstractmethod
    def translate(self, document: Mapping[str, object]) -> Contract:
        """Translate supported input into a source-neutral contract."""
        raise NotImplementedError


class Generator(Protocol):
    """Protocol for capability-negotiated generation plugins."""

    api_version: int
    requires: frozenset[str]

    @abstractmethod
    def generate(self, contract: Contract, package: str) -> dict[str, str]:
        """Generate owned output artifacts for the selected contract."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Selected contract paired with its plugin audit report."""

    contract: Contract
    report: dict[str, object]


class Selector(Protocol):
    """Protocol for plugins that reduce a normalized operation surface."""

    api_version: int
    requires: frozenset[str]

    @abstractmethod
    def select(
        self, contract: Contract, definition: Mapping[str, object] | None
    ) -> SelectionResult:
        """Select unchanged operations and produce an audit report."""
        raise NotImplementedError


class Registry:
    """Explicit registration only. Config never imports or executes Python."""

    def __init__(self) -> None:
        """Validate inputs and bind service dependencies."""
        self.translators: dict[str, Translator] = {}
        self.generators: dict[str, Generator] = {}
        self.selectors: dict[str, Selector] = {}

    def translator(self, name: str, plugin: Translator) -> None:
        """Register a compatible translator under a unique name."""
        self._check(name, plugin.api_version, self.translators)
        self.translators[name] = plugin

    def generator(self, name: str, plugin: Generator) -> None:
        """Register a compatible generator under a unique name."""
        self._check(name, plugin.api_version, self.generators)
        self.generators[name] = plugin

    def selector(self, name: str, plugin: Selector) -> None:
        """Register a compatible selector under a unique name."""
        self._check(name, plugin.api_version, self.selectors)
        self.selectors[name] = plugin

    def selection(self, source: str, name: str) -> Selector:
        """Resolve a selector and verify source capability requirements."""
        try:
            selector = self.selectors[name]
            translator = self.translators[source]
        except KeyError:
            raise CodaError("unknown source or selection plugin") from None
        if not selector.requires <= translator.capabilities:
            raise CodaError("selection plugin capabilities are incompatible")
        return selector

    @staticmethod
    def _check(name: str, version: int, existing: Mapping[str, object]) -> None:
        """Reject incompatible API versions and duplicate registration names."""
        if type(version) is not int or version != API_VERSION or name in existing:
            raise CodaError("incompatible plugin API version or duplicate registration")

    def negotiate(
        self, source: str, targets: tuple[str, ...]
    ) -> tuple[Translator, list[Generator]]:
        """Resolve a compatible translator and its requested generators."""
        try:
            translator = self.translators[source]
            generators = [self.generators[name] for name in targets]
        except KeyError:
            raise CodaError("unknown source or target plugin") from None
        if any(not generator.requires <= translator.capabilities for generator in generators):
            raise CodaError("plugin capabilities are incompatible")
        return translator, generators
