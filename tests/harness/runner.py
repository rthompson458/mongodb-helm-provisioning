"""Command execution, async polling, timing, and PASS/FAIL reporting.

HarnessRunner is deliberately infrastructure-agnostic. Scenario files describe
*what* to test; this class owns *how* a numbered test executes, how asynchronous
controller/admin operations are correlated and polled, and how evidence is
rendered. Keeping command mechanics here prevents each scenario from inventing
its own subprocess, timeout, numbering, and failure-reporting behavior.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence

from privateWorkerReplacement.async_operations import list_operation_records

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
        self._canonical_candidate = 0

    def start_profile(self, name: str) -> None:
        """Start timing one major scenario group."""

        self._profile_started[name] = time.monotonic()

    def finish_profile(self, name: str) -> None:
        """Record elapsed time for one major scenario group."""

        started = self._profile_started.pop(name, None)
        if started is not None:
            self.profile_times[name] = time.monotonic() - started

    @property
    def selective(self) -> bool:
        """Return True when --testList selected canonical full-suite test IDs."""

        return self.context.selected_tests is not None

    def set_canonical_position(self, previous_test_number: int) -> None:
        """Set the full-suite test number immediately before the next scenario."""

        if self.selective:
            self._canonical_candidate = previous_test_number

    def is_test_selected(self, number: int) -> bool:
        """Return whether one canonical test should execute in this run."""

        if not self.selective:
            return True
        selected = self.context.selected_tests or frozenset()
        return number in selected

    def any_test_selected(self, start: int, end: int) -> bool:
        """Return whether any selected test falls in an inclusive canonical range."""

        if not self.selective:
            return True
        selected = self.context.selected_tests or frozenset()
        return any(start <= number <= end for number in selected)

    def _begin_test(self, name: str) -> tuple[int, bool]:
        """Reserve one test number and decide whether its action should execute."""

        if self.selective:
            self._canonical_candidate += 1
            number = self._canonical_candidate
            return number, self.is_test_selected(number)
        return len(self.results) + 1, True

    def _display_total(self) -> int:
        """Return the denominator printed beside a test number."""

        if self.selective:
            return int(self.context.canonical_total_tests or self.context.total_tests)
        return self.context.total_tests

    def _announce(self, name: str, number: int) -> None:
        """Print which numbered test is starting before a long wait begins."""

        print(f"[RUN ] Test {number} of {self._display_total()} - {name}")

    @staticmethod
    def _skipped_result(name: str) -> StepResult:
        """Return an unrecorded success placeholder for an unselected test."""

        return StepResult(
            name=name,
            passed=True,
            note="Not selected by --testList.",
        )

    def _record(
        self,
        result: StepResult,
        number: int | None = None,
    ) -> StepResult:
        """Store a result and print a numbered human-readable status line."""

        self.results.append(result)
        display_number = number if number is not None else len(self.results)
        label = "PASS" if result.passed else "FAIL"
        print(
            f"[{label}] Test {display_number} of {self._display_total()} - "
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

        number, execute = self._begin_test(name)
        if not execute:
            return self._skipped_result(name)

        self._announce(name, number)
        return self._record(
            StepResult(
                name=name,
                passed=passed,
                note=note,
                elapsed_seconds=elapsed_seconds,
            ),
            number,
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
        env: Mapping[str, str] | None = None,
    ) -> StepResult:
        """Run a command and verify its exit code and optional output text."""

        number: int | None = None
        if announce:
            number, execute = self._begin_test(name)
            if not execute:
                return self._skipped_result(name)
            self._announce(name, number)

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
                env=dict(env) if env is not None else None,
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
                ),
                number,
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
            ),
            number,
        )

    def controller(
        self,
        name: str,
        *arguments: str,
        expect_success: bool = True,
        expected_text: str | None = None,
        timeout: int | None = None,
    ) -> StepResult:
        """Run a synchronous privateWorkerReplacement.py command."""

        command = [
            self.context.python,
            "privateWorkerReplacement.py",
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

    def admin(
        self,
        name: str,
        *arguments: str,
        expect_success: bool = True,
        expected_text: str | None = None,
        timeout: int | None = None,
    ) -> StepResult:
        """Run a foreground admin command.

        Use this for read-only diagnostics and immediate validation failures.
        Long-running administrator mutations must use admin_async() so the
        harness proves detached-worker and operation-journal behavior too.
        """

        command = [
            self.context.python,
            "privateWorkerReplacementAdmin.py",
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

    def _start_async(
        self,
        entrypoint: str,
        *arguments: str,
    ) -> AsyncOperation:
        """Submit one async CLI command and correlate its new journal entry.

        Both customer and administrator CLIs use the same detached-operation
        journal. The only difference is which executable receives the request.
        Keeping correlation here prevents two copies of subtle operation-ID
        matching logic from drifting apart.
        """

        before_ids = {
            str(item.get("operation_id", ""))
            for item in list_operation_records(self.context.config_path)
        }

        command = [
            self.context.python,
            entrypoint,
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

        operation_id = ""
        if completed.returncode == 0 and arguments:
            command_name = str(arguments[0])
            candidates = [
                item
                for item in list_operation_records(self.context.config_path)
                if str(item.get("operation_id", "")) not in before_ids
                and str(item.get("command", "")) == command_name
            ]
            if candidates:
                operation_id = str(candidates[0].get("operation_id", ""))

        stderr = completed.stderr
        if completed.returncode != 0:
            stderr += f"\nAsync submission exit code: {completed.returncode}"

        return AsyncOperation(
            operation_id=operation_id,
            command=command,
            stdout=completed.stdout,
            stderr=stderr,
        )

    def start_async_controller(self, *arguments: str) -> AsyncOperation:
        """Submit a customer async command and correlate its private journal entry.

        The customer CLI intentionally hides Operation IDs. The live acceptance
        harness is engineering tooling, so it reads the private journal only to
        correlate the request before polling through the administrator CLI.
        """

        return self._start_async("privateWorkerReplacement.py", *arguments)

    def start_async_admin(self, *arguments: str) -> AsyncOperation:
        """Submit an administrator async command and correlate its journal entry."""

        return self._start_async("privateWorkerReplacementAdmin.py", *arguments)

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
        test_number: int | None = None,
    ) -> StepResult:
        """Poll the administrator operation journal until a terminal result appears."""

        number = test_number
        if announce:
            number, execute = self._begin_test(name)
            if not execute:
                return self._skipped_result(name)
            self._announce(name, number)
        started = started_at if started_at is not None else time.monotonic()

        if not operation.operation_id:
            return self._record(
                StepResult(
                    name=name,
                    passed=False,
                    command=operation.command,
                    stdout=operation.stdout,
                    stderr=operation.stderr,
                    note="Async command did not create a correlatable operation journal entry.",
                    elapsed_seconds=time.monotonic() - started,
                ),
                number,
            )

        status_command = [
            self.context.python,
            "privateWorkerReplacementAdmin.py",
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
                    ),
                    number,
                )

            now = time.monotonic()
            if now >= next_wait_report:
                wait_number = number if number is not None else len(self.results) + 1
                print(
                    f"[WAIT] Test {wait_number} of {self._display_total()} - "
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
            ),
            number,
        )

    def _run_async_test(
        self,
        name: str,
        submitter,
        arguments: tuple[str, ...],
        *,
        timeout: int,
        expect_success: bool,
        expected_text: str | None,
    ) -> StepResult:
        """Run one numbered async test using a supplied CLI submission function."""

        number, execute = self._begin_test(name)
        if not execute:
            return self._skipped_result(name)

        self._announce(name, number)
        started = time.monotonic()
        operation = submitter(*arguments)
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
            test_number=number,
        )

    def controller_async(
        self,
        name: str,
        *arguments: str,
        timeout: int,
        expect_success: bool = True,
        expected_text: str | None = None,
    ) -> StepResult:
        """Submit a customer async command and poll its terminal result."""

        return self._run_async_test(
            name,
            self.start_async_controller,
            arguments,
            timeout=timeout,
            expect_success=expect_success,
            expected_text=expected_text,
        )

    def admin_async(
        self,
        name: str,
        *arguments: str,
        timeout: int,
        expect_success: bool = True,
        expected_text: str | None = None,
    ) -> StepResult:
        """Submit an administrator async command and poll its terminal result."""

        return self._run_async_test(
            name,
            self.start_async_admin,
            arguments,
            timeout=timeout,
            expect_success=expect_success,
            expected_text=expected_text,
        )

    def summary(self) -> int:
        """Print final pass/fail counts plus profile and total elapsed time."""

        passed = sum(result.passed for result in self.results)
        failed = len(self.results) - passed
        total_elapsed = time.monotonic() - self.started_at

        print()
        print("=" * 68)
        print(f"HARNESS SUMMARY: {passed} passed / {failed} failed")
        if self.selective:
            selected = ",".join(
                str(number)
                for number in sorted(self.context.selected_tests or frozenset())
            )
            print(f"Selected tests:     {selected}")
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
