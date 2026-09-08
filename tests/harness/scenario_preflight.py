"""Read-only preflight scenario for the live harness."""

from __future__ import annotations

from .runner import HarnessRunner


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
