"""Error hierarchy shared by generation and runtime code."""

from __future__ import annotations


class CodapactError(Exception):
    """Base class for every first-party error."""


class ContractError(CodapactError):
    """A source contract is malformed or uses an unsupported feature."""


class ModelError(CodapactError):
    """A semantic model document is invalid or has an unsupported version."""


class ConfigError(CodapactError):
    """Generation or runtime configuration is invalid."""


class PluginError(CodapactError):
    """A plugin is unknown, incompatible, or returned an invalid response."""


class CapabilityError(PluginError):
    """A target cannot consume features present in the shaped model."""

    def __init__(self, target: str, missing: frozenset[str]) -> None:
        """Name the target and every missing capability."""
        self.target = target
        self.missing = missing
        super().__init__(f"target {target!r} lacks capabilities: {', '.join(sorted(missing))}")


class OwnershipError(CodapactError):
    """Regeneration would overwrite or delete a file the generator does not own."""


class TransportError(CodapactError):
    """A request could not be sent or the response was rejected before decoding."""


class ResponseTooLargeError(TransportError):
    """The response body is larger than the client's ``max_bytes`` limit."""

    def __init__(self, limit: int) -> None:
        """Record the limit that was exceeded."""
        super().__init__(f"response exceeds {limit} bytes")
        self.limit = limit


def expect[T](value: T | None, what: str) -> T:
    """Return ``value`` or raise when an internal invariant is broken."""
    if value is None:
        raise CodapactError(f"internal invariant violated: missing {what}")
    return value
