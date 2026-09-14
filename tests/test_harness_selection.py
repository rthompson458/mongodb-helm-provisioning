"""Regression tests for selective canonical live-harness execution."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from harness.models import HarnessContext
from harness.runner import HarnessRunner


class HarnessRunnerSelectionTests(unittest.TestCase):
    """Verify --testList skips commands without shifting canonical test IDs."""

    def _runner(self, selected: frozenset[int]) -> HarnessRunner:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = root / "dev.config"
        config.write_text("[dummy]\n", encoding="utf-8")
        context = HarnessContext(
            repo_root=root,
            config_path=config,
            python="python3",
            run_id="test",
            verbose=False,
            total_tests=100,
            selected_tests=selected,
            canonical_total_tests=100,
        )
        return HarnessRunner(context)

    def test_unselected_tests_are_not_run_or_recorded(self) -> None:
        runner = self._runner(frozenset({97, 99}))
        runner.set_canonical_position(95)

        output = io.StringIO()
        with (
            redirect_stdout(output),
            mock.patch("harness.runner.subprocess.run") as run_mock,
        ):
            skipped_96 = runner.check("Test ninety-six", False)
            ran_97 = runner.check("Test ninety-seven", True)
            skipped_98 = runner.run("Test ninety-eight", ["dangerous-command"])
            ran_99 = runner.check("Test ninety-nine", True)

        self.assertTrue(skipped_96.passed)
        self.assertTrue(ran_97.passed)
        self.assertTrue(skipped_98.passed)
        self.assertTrue(ran_99.passed)
        run_mock.assert_not_called()
        self.assertEqual(len(runner.results), 2)

        text = output.getvalue()
        self.assertIn("Test 97 of 100 - Test ninety-seven", text)
        self.assertIn("Test 99 of 100 - Test ninety-nine", text)
        self.assertNotIn("Test 96 of 100", text)
        self.assertNotIn("Test 98 of 100", text)

    def test_range_helper_reports_only_selected_canonical_tests(self) -> None:
        runner = self._runner(frozenset({97, 98, 99, 100}))

        self.assertFalse(runner.any_test_selected(1, 38))
        self.assertFalse(runner.any_test_selected(39, 96))
        self.assertTrue(runner.any_test_selected(97, 100))
        self.assertTrue(runner.is_test_selected(100))
        self.assertFalse(runner.is_test_selected(96))

    def test_normal_runner_still_records_compact_numbers(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = root / "dev.config"
        config.write_text("[dummy]\n", encoding="utf-8")
        runner = HarnessRunner(
            HarnessContext(
                repo_root=root,
                config_path=config,
                python="python3",
                run_id="test",
                verbose=False,
                total_tests=2,
            )
        )

        output = io.StringIO()
        with redirect_stdout(output):
            runner.check("First", True)
            runner.check("Second", True)

        text = output.getvalue()
        self.assertIn("Test 1 of 2 - First", text)
        self.assertIn("Test 2 of 2 - Second", text)


if __name__ == "__main__":
    unittest.main()
