"""Regression tests for the live-harness command-line interface."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "tests" / "run_harness.py"


class HarnessCliTests(unittest.TestCase):
    """Keep the live harness intentional, readable, and safe by default."""

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HARNESS), *arguments],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_no_arguments_prints_help_and_runs_nothing(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout)
        self.assertIn("FULL GAUNTLET", result.stdout)
        self.assertIn("--profile", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertNotIn("privateWorkerReplacement Live Test Harness\n=", result.stdout)

    def test_help_explains_profiles_safety_and_default_config(self) -> None:
        result = self._run("--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn("preflight    5 read-only checks", result.stdout)
        self.assertIn("all         34 checks total", result.stdout)
        self.assertIn("--allow-changes", result.stdout)
        self.assertNotIn("--allow-mutations", result.stdout)
        self.assertNotIn("--allow-destructive", result.stdout)
        self.assertIn("./dev.config", result.stdout)
        self.assertIn(
            "python3 tests/run_harness.py --profile all --allow-changes",
            result.stdout,
        )

    def test_engineering_only_python_and_suffix_options_are_not_public(self) -> None:
        result = self._run("--help")

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("--python", result.stdout)
        self.assertNotIn("--suffix", result.stdout)

    def test_profile_is_required_for_an_actual_run(self) -> None:
        result = self._run("--verbose")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--profile", result.stderr)

    def test_lifecycle_profile_requires_allow_changes(self) -> None:
        result = self._run("--profile", "replicaset")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("create, modify, and delete temporary test resources", result.stderr)
        self.assertIn("--allow-changes", result.stderr)

    def test_old_dual_safety_flags_are_rejected(self) -> None:
        result = self._run(
            "--profile",
            "replicaset",
            "--allow-mutations",
            "--allow-destructive",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
