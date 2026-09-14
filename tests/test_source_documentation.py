"""Regression test enforcing intern-readable production Python documentation.

The project intentionally uses module/function/class docstrings as the first
layer of maintainer documentation. This test keeps that baseline from silently
eroding as new helpers are added.
"""

# MAINTAINER READING GUIDE
# Checks that important source modules retain maintainer-oriented documentation and architecture boundaries.
# Treat these tests as executable design documentation. A failing assertion
# should identify which controller contract changed, not merely that text moved.


from __future__ import annotations

import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _maintainer_facing_python_files() -> list[Path]:
    """Return production and live-harness sources that require docstrings.

    Ordinary unit tests are intentionally excluded. The live harness is included
    because it is operational engineering code that maintainers read and run
    directly against the real environment.
    """

    files = [
        REPO_ROOT / "privateWorkerReplacement.py",
        REPO_ROOT / "privateWorkerReplacementAdmin.py",
        REPO_ROOT / "tests" / "run_harness.py",
    ]
    files.extend(sorted((REPO_ROOT / "privateWorkerReplacement").glob("*.py")))
    files.extend(sorted((REPO_ROOT / "tests" / "harness").glob("*.py")))
    return files


class SourceDocumentationTests(unittest.TestCase):
    """Require docstrings on maintainer-facing classes and functions."""

    def test_maintainer_facing_functions_and_classes_have_docstrings(self) -> None:
        """Fail with exact file/line locations when maintainability regresses."""

        missing: list[str] = []

        for path in _maintainer_facing_python_files():
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
            "Maintainer-facing functions/classes missing docstrings:\n"
            + "\n".join(missing),
        )


if __name__ == "__main__":
    unittest.main()
