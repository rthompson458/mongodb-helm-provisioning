"""Regression test enforcing intern-readable production Python documentation.

The project intentionally uses module/function/class docstrings as the first
layer of maintainer documentation. This test keeps that baseline from silently
eroding as new helpers are added.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _production_python_files() -> list[Path]:
    """Return controller Python sources while excluding tests and generated files."""

    files = [
        REPO_ROOT / "privateWorkerReplacement.py",
        REPO_ROOT / "privateWorkerReplacementAdmin.py",
    ]
    files.extend(sorted((REPO_ROOT / "privateWorkerReplacement").glob("*.py")))
    return files


class SourceDocumentationTests(unittest.TestCase):
    """Require docstrings on every production class and function definition."""

    def test_production_functions_and_classes_have_docstrings(self) -> None:
        """Fail with exact file/line locations when maintainability regresses."""

        missing: list[str] = []

        for path in _production_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    continue
                if ast.get_docstring(node):
                    continue
                relative = path.relative_to(REPO_ROOT)
                missing.append(f"{relative}:{node.lineno} {node.name}")

        self.assertEqual(
            missing,
            [],
            "Production functions/classes missing docstrings:\n"
            + "\n".join(missing),
        )


if __name__ == "__main__":
    unittest.main()
