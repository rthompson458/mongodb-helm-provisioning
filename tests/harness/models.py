"""Small data objects shared by the live test harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StepResult:
    """Outcome of one command/check performed by the harness."""

    name: str
    passed: bool
    command: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    note: str = ""
    elapsed_seconds: float = 0.0


@dataclass
class AsyncOperation:
    """Accepted terraformController asynchronous operation."""

    operation_id: str
    command: list[str]
    stdout: str = ""
    stderr: str = ""


@dataclass
class HarnessContext:
    """Configuration and generated names shared by live scenarios."""

    repo_root: Path
    config_path: Path
    python: str
    run_id: str
    verbose: bool
    total_tests: int

    @property
    def replica_set(self) -> str:
        return f"RSTest-{self.run_id}"

    @property
    def sharded_cluster(self) -> str:
        return f"SCTest-{self.run_id}"

    @property
    def lock_cluster(self) -> str:
        return f"LockTest-{self.run_id}"

    @property
    def database(self) -> str:
        return f"DBTest_{self.run_id}"
