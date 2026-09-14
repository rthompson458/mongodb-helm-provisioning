"""Live concurrency test for ShardedCluster mutation protection.

Canonical full-suite test intent:
33. Create a one-shard cluster used only for concurrency/locking validation.
34. Observe the Terraform-created deployment lock while AddShard is active.
35. Prove a conflicting AddDatabase request is blocked during AddShard.
36. Wait for the background AddShard operation to complete successfully.
37. Verify the cluster is readable and no active change remains.
38. Delete the lock-test ShardedCluster and clean up its resources.
"""

# MAINTAINER READING GUIDE
# Live concurrency/locking scenarios. These prove overlapping operations are serialized or rejected instead of corrupting desired state.
# Treat these tests as executable design documentation. A failing assertion
# should identify which controller contract changed, not merely that text moved.


from __future__ import annotations

import subprocess
import time

from privateWorkerReplacement import kube
from privateWorkerReplacement.async_operations import effective_result, load_operation
from privateWorkerReplacement.config import load_config
from privateWorkerReplacement.deployment_lock import lock_name

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


# This scenario intentionally creates overlap. Preserve the timing/order being
# tested: a second mutation must never replay stale desired state over a newer
# successful change.
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

    # TEST 33 - Create the ShardedCluster used for concurrency testing.
    # WHY: The locking tests need a real live deployment that can undergo a topology change.
    # PASS: The one-shard ShardedCluster is created successfully.
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

    # Tests 34-36 share one background AddShard operation. Start it only when
    # at least one of those canonical tests was explicitly selected. This keeps
    # --testList from launching hidden setup work for unrelated locking tests.
    if runner.any_test_selected(34, 36):
        operation = runner.start_async_controller("AddShard", sc, "1")
        lock_started = time.monotonic()
        lock_seen = _wait_for_lock(runner, sc.lower(), operation)
    else:
        operation = AsyncOperation(operation_id="", command=[])
        lock_started = time.monotonic()
        lock_seen = False
    # TEST 34 - Observe the deployment lock while AddShard is active.
    # WHY: Proves the topology workflow publishes the lock that protects the deployment from conflicts.
    # PASS: The expected lock ConfigMap becomes visible before AddShard finishes.
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

    # TEST 35 - Reject a conflicting AddDatabase request during AddShard.
    # WHY: Proves another customer mutation cannot race an active topology change on the same cluster.
    # PASS: AddDatabase is refused while the first operation owns the deployment.
    if lock_seen:
        runner.controller(
            "Concurrent AddDatabase request is blocked during AddShard",
            "AddDatabase",
            sc,
            db,
            expect_success=False,
            expected_text="already has a controller operation in progress",
        )
    else:
        # Always consume canonical Test 35 so later test IDs never shift. In a
        # normal run this records a secondary failure explaining why the
        # concurrency assertion could not be exercised.
        runner.check(
            "Concurrent AddDatabase request is blocked during AddShard",
            False,
            "Cannot test the conflicting request because the deployment lock "
            "was not observed.",
        )

    # TEST 36 - Wait for the background AddShard operation to finish.
    # WHY: Proves the protected topology operation still completes normally after the concurrency check.
    # PASS: The asynchronous AddShard operation reaches Succeeded.
    runner.wait_async(
        "Background AddShard completes",
        operation,
        timeout=2400,
    )

    # TEST 37 - Verify the cluster is healthy after the locked operation.
    # WHY: Proves the lock is not left active and the cluster returns to normal readable service.
    # PASS: ListShards reports no active managed change.
    runner.controller(
        "Lock-test cluster returns to readable status",
        "ListShards",
        sc,
        expected_text="Active change:   None",
    )

    # TEST 38 - Delete the locking-test ShardedCluster.
    # WHY: Proves the concurrency scenario leaves a deployment that can still be torn down cleanly.
    # PASS: The asynchronous DeleteShardedCluster operation completes successfully.
    runner.controller_async(
        "Delete lock-test ShardedCluster",
        "DeleteShardedCluster",
        sc,
        "--confirm",
        timeout=2400,
    )
