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
    suffix: str
    allow_mutations: bool
    allow_destructive: bool
    verbose: bool
    total_tests: int

    @property
    def replica_set(self) -> str:
        return f"THRS-{self.suffix}"

    @property
    def sharded_cluster(self) -> str:
        return f"THSC-{self.suffix}"

    @property
    def lock_cluster(self) -> str:
        return f"THLOCK-{self.suffix}"

    @property
    def database(self) -> str:
        return f"THDB_{self.suffix}"
