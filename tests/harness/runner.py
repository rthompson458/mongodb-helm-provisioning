"""Command execution and PASS/FAIL reporting for the live harness."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from .models import HarnessContext, StepResult


class HarnessRunner:
    """Run commands and collect results without hiding useful diagnostics."""

    def __init__(self, context: HarnessContext):
        self.context = context
        self.results: list[StepResult] = []

    def _record(self, result: StepResult) -> StepResult:
        """Store a result and print a short human-readable status line."""

        self.results.append(result)
        label = "PASS" if result.passed else "FAIL"
        print(f"[{label}] {result.name}")
        if result.note:
            print(f"       {result.note}")
        if self.context.verbose or not result.passed:
            if result.command:
                print("       $ " + " ".join(result.command))
            if result.stdout.strip():
                print("       stdout:")
                for line in result.stdout.rstrip().splitlines():
                    print(f"         {line}")
            if result.stderr.strip():
                print("       stderr:")
                for line in result.stderr.rstrip().splitlines():
                    print(f"         {line}")
        return result

    def check(self, name: str, passed: bool, note: str = "") -> StepResult:
        """Record a check that does not map directly to one foreground command."""

        return self._record(
            StepResult(name=name, passed=passed, note=note)
        )

    def run(
        self,
        name: str,
        command: Sequence[str],
        *,
        expect_success: bool = True,
        expected_text: str | None = None,
        timeout: int | None = None,
    ) -> StepResult:
        """Run a command and verify its exit code and optional output text."""

        cmd = list(command)
        try:
            completed = subprocess.run(
                cmd,
                cwd=self.context.repo_root,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return self._record(
                StepResult(
                    name=name,
                    passed=False,
                    command=cmd,
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "",
                    note=f"Timed out after {timeout} seconds.",
                )
            )

        exit_ok = (
            completed.returncode == 0
            if expect_success
            else completed.returncode != 0
        )
        combined = completed.stdout + "\n" + completed.stderr
        text_ok = expected_text is None or expected_text in combined
        passed = exit_ok and text_ok

        note_parts = []
        if not exit_ok:
            wanted = "success" if expect_success else "failure"
            note_parts.append(
                f"Expected {wanted}; exit code was {completed.returncode}."
            )
        if not text_ok:
            note_parts.append(
                f"Expected output to contain: {expected_text!r}."
            )

        return self._record(
            StepResult(
                name=name,
                passed=passed,
                command=cmd,
                stdout=completed.stdout,
                stderr=completed.stderr,
                note=" ".join(note_parts),
            )
        )

    def controller(
        self,
        name: str,
        *arguments: str,
        expect_success: bool = True,
        expected_text: str | None = None,
        timeout: int | None = None,
    ) -> StepResult:
        """Run terraformController.py with the harness configuration file."""

        command = [
            self.context.python,
            "terraformController.py",
            "--config",
            str(self.context.config_path),
            *arguments,
        ]
        return self.run(
            name,
            command,
            expect_success=expect_success,
            expected_text=expected_text,
            timeout=timeout,
        )

    def start_controller(
        self, *arguments: str
    ) -> subprocess.Popen[str]:
        """Start a controller command in the background for concurrency tests."""

        command = [
            self.context.python,
            "terraformController.py",
            "--config",
            str(self.context.config_path),
            *arguments,
        ]
        return subprocess.Popen(
            command,
            cwd=self.context.repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def record_background(
        self,
        name: str,
        process: subprocess.Popen[str],
        *,
        timeout: int,
        expected_text: str | None = None,
    ) -> StepResult:
        """Wait for a background controller process and record its result."""

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            return self._record(
                StepResult(
                    name=name,
                    passed=False,
                    stdout=stdout,
                    stderr=stderr,
                    note=f"Background command timed out after {timeout} seconds.",
                )
            )

        combined = stdout + "\n" + stderr
        passed = process.returncode == 0 and (
            expected_text is None or expected_text in combined
        )
        return self._record(
            StepResult(
                name=name,
                passed=passed,
                stdout=stdout,
                stderr=stderr,
                note=(
                    ""
                    if passed
                    else f"Background exit code was {process.returncode}."
                ),
            )
        )

    def summary(self) -> int:
        """Print the final harness summary and return a shell-friendly exit code."""

        passed = sum(result.passed for result in self.results)
        failed = len(self.results) - passed
        print()
        print("=" * 68)
        print(f"HARNESS SUMMARY: {passed} passed / {failed} failed")
        print("=" * 68)
        if failed:
            print("Failed steps:")
            for result in self.results:
                if not result.passed:
                    print(f"  - {result.name}")
        return 0 if failed == 0 else 1
