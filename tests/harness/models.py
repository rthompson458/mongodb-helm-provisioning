"""Small data objects shared by the live test harness."""

# MAINTAINER READING GUIDE
# Small data models shared by the live harness. Keep these as passive data containers; execution belongs in runner/scenario modules.
# Treat these tests as executable design documentation. A failing assertion
# should identify which controller contract changed, not merely that text moved.


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
    """Accepted privateWorkerReplacement asynchronous operation."""

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
    selected_tests: frozenset[int] | None = None
    canonical_total_tests: int | None = None

    @property
    def replica_set(self) -> str:
        """Return the generated ReplicaSet fixture name for this harness run."""

        return f"RSTest-{self.run_id}"

    @property
    def sharded_cluster(self) -> str:
        """Return the generated ShardedCluster fixture name for this run."""

        return f"SCTest-{self.run_id}"

    @property
    def lock_cluster(self) -> str:
        """Return the generated locking/concurrency fixture name."""

        return f"LockTest-{self.run_id}"

    @property
    def database(self) -> str:
        """Return the generated database fixture name for this harness run."""

        return f"DBTest_{self.run_id}"
