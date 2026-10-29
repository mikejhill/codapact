"""Verify built distribution metadata against the intended release tag."""

import os
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


def verify() -> None:
    version = tomllib.loads(Path("pyproject.toml").read_text("utf-8"))["project"]["version"]
    tag = os.environ.get("RELEASE_TAG", "v" + version)
    if tag != "v" + version:
        raise ValueError("release tag and project version differ")
    wheels = list(Path("dist").glob("*.whl"))
    sources = list(Path("dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("expected exactly one wheel and one source distribution")
    with zipfile.ZipFile(wheels[0]) as archive:
        metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        wheel_version = BytesParser().parsebytes(archive.read(metadata))["Version"]
    with tarfile.open(sources[0]) as archive:
        member = next(item for item in archive.getmembers() if item.name.endswith("/PKG-INFO"))
        handle = archive.extractfile(member)
        if handle is None:
            raise ValueError("source metadata is missing")
        source_version = BytesParser().parsebytes(handle.read())["Version"]
    if wheel_version != version or source_version != version:
        raise ValueError("distribution versions differ from project version")
    print(f"Verified wheel and source distribution version {version}")


if __name__ == "__main__":
    verify()
