"""Read-only live preflight checks.

Canonical full-suite test intent:
1. Prove the customer CLI can render help without starting work.
2. Prove deployment inventory is readable from the configured environment.
3. Prove global shard status is readable before lifecycle work begins.
4. Prove the Terraform executable required by lifecycle operations is available.
5. Prove kubectl is available for Kubernetes status and lifecycle checks.
"""

from __future__ import annotations

from .runner import HarnessRunner

TEST_COUNT = 5


def run(runner: HarnessRunner) -> None:
    """Verify the CLI and basic status paths work without changing anything."""

    # TEST 1 - Render customer CLI help.
    # WHY: Proves the public entry point is installed and its command parser can start safely.
    # PASS: Help text renders successfully and identifies the Terraform-driven DBaaS controller.
    runner.controller(
        "CLI help renders",
        "--help",
        expected_text="Terraform-driven MongoDB DBaaS controller",
    )
    # TEST 2 - Read deployment inventory.
    # WHY: Proves the controller can read the configured environment before lifecycle work begins.
    # PASS: ListDeployments completes successfully without changing any managed resources.
    runner.controller(
        "ListDeployments is readable",
        "ListDeployments",
    )
    # TEST 3 - Read global shard status.
    # WHY: Proves ShardedCluster status can be queried before any test topology is created.
    # PASS: ListShards completes successfully as a read-only operation.
    runner.controller(
        "Global ListShards is readable",
        "ListShards",
    )
    # TEST 4 - Verify Terraform is available.
    # WHY: Every managed lifecycle mutation depends on the local Terraform executable.
    # PASS: `terraform version` runs successfully.
    runner.run(
        "Terraform executable is available",
        ["terraform", "version"],
    )
    # TEST 5 - Verify kubectl is available.
    # WHY: Status checks, recovery checks, and Kubernetes validation require kubectl.
    # PASS: `kubectl version --client` runs successfully.
    runner.run(
        "kubectl executable is available",
        ["kubectl", "version", "--client"],
    )
