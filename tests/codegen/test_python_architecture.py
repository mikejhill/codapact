"""Protect the requested Python architecture in shipped and generated source modules."""

from __future__ import annotations

import ast
from pathlib import Path

from codapact.codegen.project import ProjectGenerator


class TestPythonArchitecture:
    """Keep service behavior in classes and preserve explicit source contracts."""

    def test_shipped_modules_have_no_any_or_free_functions(self) -> None:
        """Every shipped module documents its purpose and avoids permissive public signatures."""
        root = Path(__file__).resolve().parents[2] / "src/codapact"
        errors: list[str] = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            if not bool(ast.get_docstring(tree)):
                errors.append(f"{path.name}: missing module docstring")
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    errors.append(f"{path.name}: module function {node.name}")
            if any(isinstance(node, ast.Name) and node.id == "Any" for node in ast.walk(tree)):
                errors.append(f"{path.name}: Any annotation")
            if not any(
                isinstance(node, ast.ImportFrom)
                and node.module == "__future__"
                and any(alias.name == "annotations" for alias in node.names)
                for node in tree.body
            ):
                errors.append(f"{path.name}: missing future annotations")

        assert not errors, "\n".join(errors)

    def test_generated_modules_keep_the_same_class_and_typing_boundaries(
        self, tmp_path: Path
    ) -> None:
        """Generated clients have documented, annotated classes and defer resource reads."""
        root = Path(__file__).resolve().parents[2]
        output = tmp_path / "generated"
        ProjectGenerator.generate(
            str(root / "examples/quickstart/openapi.yaml"),
            output,
            package="architecture_pets",
        )
        errors: list[str] = []
        for path in (output / "src").rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            if not bool(ast.get_docstring(tree)):
                errors.append(f"{path.name}: missing module docstring")
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    errors.append(f"{path.name}: module function {node.name}")
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    if (
                        not isinstance(node.value.func, ast.Name)
                        or node.value.func.id != "TypedDict"
                    ):
                        errors.append(
                            f"{path.name}: import-time call other than a type declaration"
                        )
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "Any":
                    errors.append(f"{path.name}: Any annotation")
                if isinstance(node, ast.FunctionDef):
                    if node.returns is None or not bool(ast.get_docstring(node)):
                        errors.append(f"{path.name}: undocumented or untyped {node.name}")
                    arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                    if any(
                        arg.annotation is None
                        for arg in arguments
                        if arg.arg not in {"self", "cls"}
                    ):
                        errors.append(f"{path.name}: untyped argument in {node.name}")

        assert not errors, "\n".join(errors)
