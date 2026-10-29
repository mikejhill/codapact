"""Convert argparse attributes into immutable runtime command inputs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeArguments:
    """Typed runtime configuration, command, and raw request values."""

    command: str
    config: str | None
    config_dir: str | None
    auth_file: str | None
    no_auth_file: bool
    base_url: str | None
    timeout: float | None
    allow_private: bool | None
    pretty: bool
    parameters: tuple[str | None, ...]
    body: str | None

    @classmethod
    def from_namespace(cls, args: argparse.Namespace, count: int) -> RuntimeArguments:
        """Convert values whose types and presence are declared by the runtime parser."""
        return cls(
            command=str(args.command),
            config=str(args.config) if args.config is not None else None,
            config_dir=str(args.config_dir) if args.config_dir is not None else None,
            auth_file=str(args.auth_file) if args.auth_file is not None else None,
            no_auth_file=bool(args.no_auth_file),
            base_url=str(args.base_url) if args.base_url is not None else None,
            timeout=float(args.timeout) if args.timeout is not None else None,
            allow_private=bool(args.allow_private) if args.allow_private is not None else None,
            pretty=bool(args.pretty),
            parameters=tuple(
                str(getattr(args, f"p{index}")) if getattr(args, f"p{index}") is not None else None
                for index in range(count)
            ),
            body=str(args.body) if getattr(args, "body", None) is not None else None,
        )

    def overrides(self) -> dict[str, object]:
        """Return explicitly supplied ordinary runtime setting overrides."""
        return {
            key: value
            for key, value in (
                ("base_url", self.base_url),
                ("timeout", self.timeout),
                ("allow_private", self.allow_private),
            )
            if value is not None
        }
