"""End-to-end ShardedCluster and multi-shard lifecycle scenario."""

from __future__ import annotations

from .runner import HarnessRunner

TEST_COUNT = 13


def run(runner: HarnessRunner) -> None:
    """Exercise count-based shard operations, stopping on prerequisite failure."""

    ctx = runner.context
    sc = ctx.sharded_cluster
    db = ctx.database

    created = runner.controller_async(
        "Create three-shard test cluster",
        "AddShardedCluster",
        sc,
        "--shards",
        "3",
        timeout=2400,
    )
    if not created.passed:
        return

    targeted_status = runner.controller(
        "Targeted shard status works",
        "ListShards",
        sc,
        expected_text=f"{sc.lower()}-0",
    )
    if not targeted_status.passed:
        return

    global_status = runner.controller(
        "Global shard status includes test cluster",
        "ListShards",
        expected_text=sc,
    )
    if not global_status.passed:
        return

    added = runner.controller_async(
        "Add two shards in one command",
        "AddShard",
        sc,
        "2",
        timeout=2400,
    )
    if not added.passed:
        return

    five_shard_status = runner.controller(
        "Five-shard cluster reports online",
        "ListShards",
        sc,
        expected_text=f"{sc.lower()}-4",
    )
    if not five_shard_status.passed:
        return

    created_db = runner.controller(
        "Create database on ShardedCluster",
        "AddDatabase",
        sc,
        db,
        expected_text=f"ShardedCluster '{sc}'",
        timeout=900,
    )
    if not created_db.passed:
        return

    deleted_one = runner.controller_async(
        "Delete a shard while database exists",
        "DeleteShard",
        sc,
        "1",
        "--confirm",
        timeout=2400,
    )
    if not deleted_one.passed:
        return

    rotated = runner.controller(
        "Rotate ShardedCluster database credentials",
        "RotatePasswords",
        sc,
        db,
        expected_text="Rotated all three passwords",
        timeout=900,
    )
    if not rotated.passed:
        return

    disabled = runner.controller(
        "Disable ShardedCluster Owner",
        "DisableOwner",
        sc,
        db,
        "--confirm",
        expected_text="is now Disabled",
        timeout=900,
    )
    if not disabled.passed:
        return

    deleted_db = runner.controller(
        "Delete ShardedCluster database",
        "DeleteDatabase",
        sc,
        db,
        "--confirm",
        expected_text="was successfully deleted",
        timeout=900,
    )
    if not deleted_db.passed:
        return

    reduced = runner.controller_async(
        "Delete three more shards in one command and leave one",
        "DeleteShard",
        sc,
        "3",
        "--confirm",
        timeout=2400,
    )
    if not reduced.passed:
        return

    blocked_final = runner.controller_async(
        "Block deletion of final shard",
        "DeleteShard",
        sc,
        "1",
        "--confirm",
        expect_success=False,
        expected_text="must retain at least 1 shard",
        timeout=300,
    )
    if not blocked_final.passed:
        return

    runner.controller_async(
        "Delete temporary ShardedCluster",
        "DeleteShardedCluster",
        sc,
        "--confirm",
        timeout=2400,
    )
