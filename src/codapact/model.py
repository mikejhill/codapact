"""Versioned source-neutral intermediate representation and plugin contracts."""

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from codapact.errors import CodaError

API_VERSION = 1
IR_VERSION = 1


@dataclass(frozen=True)
class Operation:
    id: str
    method: str
    path: str
    parameters: tuple[dict[str, Any], ...]
    body: dict[str, Any] | None
    body_required: bool
    responses: dict[str, dict[str, Any] | None]
    security: tuple[dict[str, list[str]], ...]
    summary: str = ""


@dataclass(frozen=True)
class Contract:
    title: str
    base_url: str
    operations: tuple[Operation, ...]
    security_schemes: dict[str, dict[str, Any]]
    types: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = IR_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Translator(Protocol):
    api_version: int
    capabilities: frozenset[str]

    def translate(self, document: dict[str, Any]) -> Contract: ...


class Generator(Protocol):
    api_version: int
    requires: frozenset[str]

    def generate(self, contract: Contract, package: str) -> dict[str, str]: ...


class Registry:
    """Explicit registration only. Config never imports or executes Python."""

    def __init__(self) -> None:
        self.translators: dict[str, Translator] = {}
        self.generators: dict[str, Generator] = {}

    def translator(self, name: str, plugin: Translator) -> None:
        self._check(name, plugin.api_version, self.translators)
        self.translators[name] = plugin

    def generator(self, name: str, plugin: Generator) -> None:
        self._check(name, plugin.api_version, self.generators)
        self.generators[name] = plugin

    @staticmethod
    def _check(name: str, version: int, existing: dict[str, Any]) -> None:
        if version != API_VERSION or name in existing:
            raise CodaError("incompatible plugin API version or duplicate registration")

    def negotiate(
        self, source: str, targets: tuple[str, ...]
    ) -> tuple[Translator, list[Generator]]:
        try:
            translator = self.translators[source]
            generators = [self.generators[name] for name in targets]
        except KeyError:
            raise CodaError("unknown source or target plugin") from None
        if any(not generator.requires <= translator.capabilities for generator in generators):
            raise CodaError("plugin capabilities are incompatible")
        return translator, generators
