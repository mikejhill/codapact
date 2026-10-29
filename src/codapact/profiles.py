"""Compatibility exports; implementation lives in codapact.execution.profiles."""

from __future__ import annotations

from codapact.execution.profiles import (
    CredentialStore as CredentialStore,
)
from codapact.execution.profiles import (
    JsonFileStore as JsonFileStore,
)
from codapact.execution.profiles import (
    ProfileFiles as ProfileFiles,
)
from codapact.execution.profiles import (
    RuntimeProfiles as RuntimeProfiles,
)
from codapact.execution.profiles import (
    StoredAuth as StoredAuth,
)
from codapact.execution.profiles import (
    os as os,
)
from codapact.execution.profiles import (
    stat as stat,
)

validate_profile = RuntimeProfiles.validate_profile
config_directory = RuntimeProfiles.config_directory
read_file = ProfileFiles.read_file
runtime_settings = RuntimeProfiles.runtime_settings

__all__ = [
    "validate_profile",
    "config_directory",
    "read_file",
    "CredentialStore",
    "JsonFileStore",
    "StoredAuth",
    "runtime_settings",
    "os",
    "stat",
]
