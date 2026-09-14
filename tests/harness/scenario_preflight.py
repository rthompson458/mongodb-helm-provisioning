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

    runner.controller(
        "CLI help renders",
        "--help",
        expected_text="Terraform-driven MongoDB DBaaS controller",
    )
    runner.controller(
        "ListDeployments is readable",
        "ListDeployments",
    )
    runner.controller(
        "Global ListShards is readable",
        "ListShards",
    )
    runner.run(
        "Terraform executable is available",
        ["terraform", "version"],
    )
    runner.run(
        "kubectl executable is available",
        ["kubectl", "version", "--client"],
    )
