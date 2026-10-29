"""Validate documentation metadata, local links, and directory README indexes."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator

from codapact.exceptions import AppError
from codapact.infrastructure.diagnostics import CliDiagnostics
from codapact.infrastructure.documents import DocumentLoader


class DocumentationCheck:
    """Check structural documentation contracts without claiming editorial review."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._validator = Draft202012Validator(
            json.loads((root / "schemas/documentation.schema.json").read_text("utf-8"))
        )

    def check(self) -> list[str]:
        """Return all metadata, link, and navigation defects."""
        docs = self._root / "docs"
        paths = [self._root / "README.md", self._root / "CONTRIBUTING.md", *docs.rglob("*.md")]
        errors: list[str] = []
        for path in paths:
            text = path.read_text("utf-8")
            self._metadata(path, text, errors)
            targets = self._links(path, text, errors)
            if path.name == "README.md" and path.is_relative_to(docs):
                self._index(path, targets, errors)
        for directory in [docs, *(p for p in docs.rglob("*") if p.is_dir())]:
            if not (directory / "README.md").is_file():
                errors.append(f"{directory.relative_to(self._root)}: missing README.md")
        return errors

    def _metadata(self, path: Path, text: str, errors: list[str]) -> None:
        """Parse data-only YAML and check the local lifecycle schema."""
        match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
        if match is None:
            errors.append(f"{path.name}: missing frontmatter")
            return
        metadata = DocumentLoader.parse(match[1].encode("utf-8"))
        errors.extend(
            f"{path.relative_to(self._root)}: {error.message}"
            for error in self._validator.iter_errors(metadata)
        )

    def _links(self, path: Path, text: str, errors: list[str]) -> set[Path]:
        """Check local Markdown links while ignoring code fences and remote URLs."""
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        targets: set[Path] = set()
        for link in re.findall(r"!?\[[^\]]*\]\(([^\s)]+)\)", text):
            url = urlsplit(link)
            if url.scheme or url.netloc or not url.path:
                continue
            target = (path.parent / unquote(url.path)).resolve()
            targets.add(target)
            if not target.exists():
                errors.append(f"{path.relative_to(self._root)}: missing link target {link}")
        return targets

    def _index(self, path: Path, targets: set[Path], errors: list[str]) -> None:
        """Require a direct link to every child document, directory, and asset."""
        for child in path.parent.iterdir():
            if child == path:
                continue
            target = child / "README.md" if child.is_dir() else child
            if target.resolve() not in targets and child.resolve() not in targets:
                errors.append(f"{path.relative_to(self._root)}: missing child link {child.name}")

    @classmethod
    def main(cls) -> int:
        """Report structural errors with a failing exit status."""
        logger = CliDiagnostics.configure(__name__)
        try:
            errors = cls(Path(__file__).resolve().parents[1]).check()
        except (AppError, OSError, ValueError) as error:
            logger.error("Documentation check failed: %s", error)
            return 1
        for error in errors:
            logger.error("%s", error)
        if errors:
            return 1
        sys.stdout.write("Documentation metadata, links, and README indexes passed.\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(DocumentationCheck.main())
