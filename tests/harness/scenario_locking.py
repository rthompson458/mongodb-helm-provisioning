"""Live concurrency test for ShardedCluster mutation protection."""

from __future__ import annotations

import subprocess
import time

from terraform_controller import kube
from terraform_controller.async_operations import effective_result, load_operation
from terraform_controller.config import load_config
from terraform_controller.deployment_lock import lock_name

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
    """Prove concurrent customer mutation is blocked while AddShard is active.

    There are two layers of protection:
    1. launch_operation blocks an obvious second async request on the same
       deployment before another detached worker is created;
    2. the Terraform-created ShardedCluster deployment lock is the underlying
       cross-process lifecycle guard and is tested directly by unit tests.

    This live scenario proves both that the real Kubernetes lock becomes visible
    and that a second customer AddDatabase request is refused while the first
    operation is still active.
    """

    ctx = runner.context
    sc = ctx.lock_cluster
    db = f"LockDB_{ctx.run_id}"

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

    # start_async_controller correlates the private Operation ID for the harness;
    # the public customer output itself still hides that identifier.
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
            "Concurrent AddDatabase request is blocked during AddShard",
            "AddDatabase",
            sc,
            db,
            expect_success=False,
            expected_text="already has a controller operation in progress",
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
