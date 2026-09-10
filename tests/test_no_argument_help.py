"""Regression tests for no-argument help behavior on both CLI entry points."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class NoArgumentHelpTests(unittest.TestCase):
    """Running either executable with no command must show help and succeed."""

    def _run_without_arguments(self, script_name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / script_name)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_public_cli_without_arguments_prints_help_and_succeeds(self) -> None:
        result = self._run_without_arguments("privateWorkerReplacement.py")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: privateWorkerReplacement.py", result.stdout)
        self.assertIn("Terraform-driven MongoDB DBaaS controller", result.stdout)
        self.assertIn("ListDatabaseAccounts", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_admin_cli_without_arguments_prints_help_and_succeeds(self) -> None:
        result = self._run_without_arguments("privateWorkerReplacementAdmin.py")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: privateWorkerReplacementAdmin.py", result.stdout)
        self.assertIn("platform administration interface", result.stdout)
        self.assertIn("ListManagedResources", result.stdout)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
