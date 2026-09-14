"""Regression tests for the live-harness command-line interface."""

from __future__ import annotations

import subprocess
import sys
import unittest

import run_harness as harness_cli
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
        self.assertIn("--admin", result.stdout)
        self.assertIn("--testList", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertNotIn("privateWorkerReplacement Live Test Harness\n=", result.stdout)

    def test_help_explains_profiles_safety_and_default_config(self) -> None:
        result = self._run("--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn("preflight    5 read-only checks", result.stdout)
        self.assertIn("replicaset  16 checks total", result.stdout)
        self.assertIn("sharded     21 checks total", result.stdout)
        self.assertIn("locking     11 checks total", result.stdout)
        self.assertIn("--admin     67 checks total when run alone", result.stdout)
        self.assertIn("all         100 checks total", result.stdout)
        self.assertIn("FULL GAUNTLET - all 100 live acceptance checks", result.stdout)
        self.assertIn("--allow-changes", result.stdout)
        self.assertNotIn("--allow-mutations", result.stdout)
        self.assertNotIn("--allow-destructive", result.stdout)
        self.assertIn("./dev.config", result.stdout)
        self.assertIn(
            "python3 tests/run_harness.py --profile all --allow-changes",
            result.stdout,
        )
        self.assertIn(
            "python3 tests/run_harness.py --testList 97-100 --allow-changes",
            result.stdout,
        )
        self.assertIn("56,58-67", result.stdout)
        self.assertIn("no spaces", result.stdout)

    def test_engineering_only_python_and_suffix_options_are_not_public(self) -> None:
        result = self._run("--help")

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("--python", result.stdout)
        self.assertNotIn("--suffix", result.stdout)

    def test_profile_or_admin_selection_is_required_for_an_actual_run(self) -> None:
        result = self._run("--verbose")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Choose --profile", result.stderr)
        self.assertIn("and/or --admin", result.stderr)

    def test_lifecycle_profile_requires_allow_changes(self) -> None:
        result = self._run("--profile", "replicaset")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("create, modify, deliberately damage", result.stderr)
        self.assertIn("--allow-changes", result.stderr)

    def test_admin_suite_requires_allow_changes(self) -> None:
        result = self._run("--admin")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deliberately damage", result.stderr)
        self.assertIn("--allow-changes", result.stderr)

    def test_help_documents_admin_command_and_clean_start_requirement(self) -> None:
        result = self._run("--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn(
            "python3 tests/run_harness.py --admin --allow-changes",
            result.stdout,
        )
        self.assertIn("requires a clean DBaaS starting inventory", result.stdout)
        self.assertIn(
            "all         100 checks total. Runs every lifecycle and administrator test",
            result.stdout,
        )

    def test_test_list_parser_accepts_single_tests_and_ranges(self) -> None:
        self.assertEqual(
            harness_cli._parse_test_list("56,58-67", 100),
            (56, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67),
        )
        self.assertEqual(
            harness_cli._parse_test_list("97-100", 100),
            (97, 98, 99, 100),
        )
        self.assertEqual(
            harness_cli._parse_test_list("58-58", 100),
            (58,),
        )

    def test_test_list_parser_rejects_spaces_descending_and_out_of_range(self) -> None:
        with self.assertRaises(harness_cli.argparse.ArgumentTypeError):
            harness_cli._parse_test_list("56, 58-67", 100)
        with self.assertRaises(harness_cli.argparse.ArgumentTypeError):
            harness_cli._parse_test_list("67-58", 100)
        with self.assertRaises(harness_cli.argparse.ArgumentTypeError):
            harness_cli._parse_test_list("0", 100)
        with self.assertRaises(harness_cli.argparse.ArgumentTypeError):
            harness_cli._parse_test_list("101", 100)

    def test_profile_and_test_list_are_mutually_exclusive(self) -> None:
        result = self._run(
            "--profile",
            "all",
            "--testList",
            "97-100",
            "--allow-changes",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not allowed with argument", result.stderr)

    def test_admin_and_test_list_are_rejected_together(self) -> None:
        result = self._run(
            "--testList",
            "97-100",
            "--admin",
            "--allow-changes",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--testList cannot be combined with --admin", result.stderr)

    def test_test_list_requires_allow_changes(self) -> None:
        result = self._run("--testList", "97-100")

        self.assertNotEqual(result.returncode, 0)
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
