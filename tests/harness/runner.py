"""Command execution, async polling, timing, and PASS/FAIL reporting."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Sequence

from .models import AsyncOperation, HarnessContext, StepResult


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class HarnessRunner:
    """Run commands and collect results without hiding useful diagnostics."""

    def __init__(self, context: HarnessContext):
        self.context = context
        self.results: list[StepResult] = []
        self.started_at = time.monotonic()
        self.profile_times: dict[str, float] = {}
        self._profile_started: dict[str, float] = {}

    def start_profile(self, name: str) -> None:
        """Start timing one major scenario group."""

        self._profile_started[name] = time.monotonic()

    def finish_profile(self, name: str) -> None:
        """Record elapsed time for one major scenario group."""

        started = self._profile_started.pop(name, None)
        if started is not None:
            self.profile_times[name] = time.monotonic() - started

    def _next_number(self) -> int:
        return len(self.results) + 1

    def _announce(self, name: str) -> None:
        """Print which numbered test is starting before a long wait begins."""

        print(
            f"[RUN ] Test {self._next_number()} of {self.context.total_tests} - {name}"
        )

    def _record(self, result: StepResult) -> StepResult:
        """Store a result and print a numbered human-readable status line."""

        self.results.append(result)
        number = len(self.results)
        label = "PASS" if result.passed else "FAIL"
        print(
            f"[{label}] Test {number} of {self.context.total_tests} - "
            f"{result.name} ({_duration(result.elapsed_seconds)})"
        )
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

    def check(
        self,
        name: str,
        passed: bool,
        note: str = "",
        *,
        elapsed_seconds: float = 0.0,
    ) -> StepResult:
        """Record a check that does not map directly to one foreground command."""

        self._announce(name)
        return self._record(
            StepResult(
                name=name,
                passed=passed,
                note=note,
                elapsed_seconds=elapsed_seconds,
            )
        )

    def run(
        self,
        name: str,
        command: Sequence[str],
        *,
        expect_success: bool = True,
        expected_text: str | None = None,
        timeout: int | None = None,
        announce: bool = True,
    ) -> StepResult:
        """Run a command and verify its exit code and optional output text."""

        if announce:
            self._announce(name)
        started = time.monotonic()
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
                    elapsed_seconds=time.monotonic() - started,
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
                elapsed_seconds=time.monotonic() - started,
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
        """Run a synchronous terraformController.py command."""

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

    def start_async_controller(self, *arguments: str) -> AsyncOperation:
        """Submit one public async controller command and return its Operation ID."""

        command = [
            self.context.python,
            "terraformController.py",
            "--config",
            str(self.context.config_path),
            *arguments,
        ]
        completed = subprocess.run(
            command,
            cwd=self.context.repo_root,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        combined = completed.stdout + "\n" + completed.stderr
        match = re.search(r"Operation ID:\s*([A-Za-z0-9_-]+)", combined)
        return AsyncOperation(
            operation_id=match.group(1) if match else "",
            command=command,
            stdout=completed.stdout,
            stderr=(
                completed.stderr
                if completed.returncode == 0
                else completed.stderr
                + f"\nAsync submission exit code: {completed.returncode}"
            ),
        )

    def wait_async(
        self,
        name: str,
        operation: AsyncOperation,
        *,
        timeout: int,
        expect_success: bool = True,
        expected_text: str | None = None,
        announce: bool = True,
        started_at: float | None = None,
    ) -> StepResult:
        """Poll ListOperation until a terminal positive/negative result appears."""

        if announce:
            self._announce(name)
        started = started_at if started_at is not None else time.monotonic()

        if not operation.operation_id:
            return self._record(
                StepResult(
                    name=name,
                    passed=False,
                    command=operation.command,
                    stdout=operation.stdout,
                    stderr=operation.stderr,
                    note="Async command did not return an Operation ID.",
                    elapsed_seconds=time.monotonic() - started,
                )
            )

        status_command = [
            self.context.python,
            "terraformController.py",
            "--config",
            str(self.context.config_path),
            "ListOperation",
            operation.operation_id,
        ]

        deadline = time.monotonic() + timeout
        next_wait_report = time.monotonic() + 60
        last_stdout = ""
        last_stderr = ""

        while time.monotonic() < deadline:
            completed = subprocess.run(
                status_command,
                cwd=self.context.repo_root,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            last_stdout = completed.stdout
            last_stderr = completed.stderr
            combined = last_stdout + "\n" + last_stderr
            result_match = re.search(r"^Result:\s*(.+?)\s*$", combined, re.MULTILINE)
            result = result_match.group(1).strip() if result_match else ""

            if result in {"Succeeded", "Failed", "Interrupted"}:
                result_ok = result == ("Succeeded" if expect_success else "Failed")
                text_ok = expected_text is None or expected_text in combined
                passed = result_ok and text_ok
                note_parts = [f"Operation ID: {operation.operation_id}."]
                if not result_ok:
                    wanted = "Succeeded" if expect_success else "Failed"
                    note_parts.append(f"Expected {wanted}; operation reported {result}.")
                if not text_ok:
                    note_parts.append(
                        f"Expected status output to contain: {expected_text!r}."
                    )
                return self._record(
                    StepResult(
                        name=name,
                        passed=passed,
                        command=operation.command,
                        stdout=operation.stdout + "\n" + last_stdout,
                        stderr=operation.stderr + "\n" + last_stderr,
                        note=" ".join(note_parts),
                        elapsed_seconds=time.monotonic() - started,
                    )
                )

            now = time.monotonic()
            if now >= next_wait_report:
                print(
                    f"[WAIT] Test {self._next_number()} of {self.context.total_tests} - "
                    f"{name} - elapsed {_duration(now - started)} - "
                    f"operation {operation.operation_id} still In Progress"
                )
                next_wait_report = now + 60

            time.sleep(5)

        return self._record(
            StepResult(
                name=name,
                passed=False,
                command=operation.command,
                stdout=operation.stdout + "\n" + last_stdout,
                stderr=operation.stderr + "\n" + last_stderr,
                note=(
                    f"Operation {operation.operation_id} did not reach a terminal "
                    f"result within {timeout} seconds."
                ),
                elapsed_seconds=time.monotonic() - started,
            )
        )

    def controller_async(
        self,
        name: str,
        *arguments: str,
        timeout: int,
        expect_success: bool = True,
        expected_text: str | None = None,
    ) -> StepResult:
        """Submit an async command, then poll its operation result for this test."""

        self._announce(name)
        started = time.monotonic()
        operation = self.start_async_controller(*arguments)
        if operation.operation_id:
            print(
                f"       Operation {operation.operation_id} accepted; "
                "polling for completion."
            )
        return self.wait_async(
            name,
            operation,
            timeout=timeout,
            expect_success=expect_success,
            expected_text=expected_text,
            announce=False,
            started_at=started,
        )

    def summary(self) -> int:
        """Print final pass/fail counts plus profile and total elapsed time."""

        passed = sum(result.passed for result in self.results)
        failed = len(self.results) - passed
        total_elapsed = time.monotonic() - self.started_at

        print()
        print("=" * 68)
        print(f"HARNESS SUMMARY: {passed} passed / {failed} failed")
        for name, seconds in self.profile_times.items():
            print(f"{name + ':':18} {_duration(seconds)}")
        print(f"{'Total elapsed:':18} {_duration(total_elapsed)}")
        print("=" * 68)
        if failed:
            print("Failed steps:")
            for result in self.results:
                if not result.passed:
                    print(f"  - {result.name}")
        return 0 if failed == 0 else 1
