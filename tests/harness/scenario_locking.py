"""Live concurrency test for the ShardedCluster deployment lock."""

from __future__ import annotations

import subprocess
import time

from terraform_controller.async_operations import effective_result, load_operation
from terraform_controller.config import load_config
from terraform_controller.deployment_lock import lock_name
from terraform_controller import kube

from .models import AsyncOperation
from .runner import HarnessRunner

TEST_COUNT = 6


def _wait_for_lock(
    runner: HarnessRunner,
    deployment_key: str,
    operation: AsyncOperation,
    seconds: int = 300,
) -> bool:
    """Poll until the Terraform-created lock appears or AddShard terminates."""

    if not operation.operation_id:
        return False

    config = load_config(runner.context.config_path)
    command = kube.base(config) + [
        "-n",
        config["mongodb_namespace"],
        "get",
        "configmap",
        lock_name(deployment_key),
        "-o",
        "name",
    ]

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            state = load_operation(runner.context.config_path, operation.operation_id)
            if effective_result(state) in {"Succeeded", "Failed", "Interrupted"}:
                return False
        except Exception:
            return False

        result = subprocess.run(
            command,
            cwd=runner.context.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return True
        time.sleep(0.25)
    return False


def run(runner: HarnessRunner) -> None:
    """Prove a concurrent database mutation is blocked during async AddShard."""

    ctx = runner.context
    sc = ctx.lock_cluster
    db = f"LOCKDB_{ctx.suffix}"

    created = runner.controller_async(
        "Create lock-test ShardedCluster",
        "AddShardedCluster",
        sc,
        "--shards",
        "1",
        timeout=2400,
    )
    if not created.passed:
        return

    # Public AddShard now returns immediately with an Operation ID. Its detached
    # worker owns the Terraform-created deployment lock while the harness starts
    # a second command to prove conflicting mutations are blocked.
    operation = runner.start_async_controller("AddShard", sc, "1")
    lock_started = time.monotonic()
    lock_seen = _wait_for_lock(runner, sc.lower(), operation)
    runner.check(
        "Observe Terraform-created deployment lock",
        lock_seen,
        (
            f"Expected ConfigMap {lock_name(sc.lower())} while AddShard was active."
            if not lock_seen
            else "Deployment lock became visible in Kubernetes."
        ),
        elapsed_seconds=time.monotonic() - lock_started,
    )

    if lock_seen:
        runner.controller(
            "Concurrent AddDatabase is blocked by SC deployment lock",
            "AddDatabase",
            sc,
            db,
            expect_success=False,
            expected_text="busy with another managed change",
        )

    runner.wait_async(
        "Background AddShard completes",
        operation,
        timeout=2400,
    )

    runner.controller(
        "Lock-test cluster returns to readable status",
        "ListShards",
        sc,
        expected_text="Active change:   None",
    )

    runner.controller_async(
        "Delete lock-test ShardedCluster",
        "DeleteShardedCluster",
        sc,
        "--confirm",
        timeout=2400,
    )
