"""Live concurrency test for the ShardedCluster deployment lock."""

from __future__ import annotations

import subprocess
import time

from terraform_controller.config import load_config
from terraform_controller.deployment_lock import lock_name
from terraform_controller import kube

from .runner import HarnessRunner


def _wait_for_lock(
    runner: HarnessRunner,
    deployment_key: str,
    process: subprocess.Popen[str],
    seconds: int = 300,
) -> bool:
    """Poll until the Terraform-created lock appears or AddShard exits.

    Terraform may need to refresh the repository and initialize providers before
    it reaches the lock operation, so this timeout is intentionally longer than
    a simple Kubernetes polling timeout.
    """

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
        if process.poll() is not None:
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
    """Prove a concurrent database mutation is blocked during AddShard."""

    ctx = runner.context
    sc = ctx.lock_cluster
    db = f"LOCKDB_{ctx.suffix}"

    created = runner.controller(
        "Create lock-test ShardedCluster",
        "AddShardedCluster",
        sc,
        "--shards",
        "1",
        expected_text=f"ShardedCluster '{sc}' was successfully created",
        timeout=2400,
    )
    if not created.passed:
        return

    # AddShard runs in the background so the harness can issue a second command
    # while the first operation owns the Terraform-created deployment lock.
    process = runner.start_controller("AddShard", sc, "1")
    lock_seen = _wait_for_lock(runner, sc.lower(), process)
    runner.check(
        "Observe Terraform-created deployment lock",
        lock_seen,
        (
            f"Expected ConfigMap {lock_name(sc.lower())} while AddShard was active."
            if not lock_seen
            else "Deployment lock became visible in Kubernetes."
        ),
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

    runner.record_background(
        "Background AddShard completes",
        process,
        timeout=2400,
        expected_text="Successfully added 1 shard(s)",
    )

    runner.controller(
        "Lock-test cluster returns to readable status",
        "ListShards",
        sc,
        expected_text="Active change:   None",
    )

    runner.controller(
        "Delete lock-test ShardedCluster",
        "DeleteShardedCluster",
        sc,
        "--confirm",
        expected_text=f"ShardedCluster '{sc}' was successfully deleted",
        timeout=2400,
    )
