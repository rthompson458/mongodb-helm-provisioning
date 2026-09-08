"""End-to-end ShardedCluster and multi-shard lifecycle scenario."""

from __future__ import annotations

from .runner import HarnessRunner


def run(runner: HarnessRunner) -> None:
    """Exercise count-based shard operations and database safety rules."""

    ctx = runner.context
    sc = ctx.sharded_cluster
    db = ctx.database

    runner.controller(
        "Create two-shard test cluster",
        "AddShardedCluster",
        sc,
        "--shards",
        "2",
        expected_text=f"ShardedCluster '{sc}' was successfully created",
        timeout=2400,
    )
    runner.controller(
        "Targeted shard status works",
        "ListShards",
        sc,
        expected_text=f"{sc.lower()}-0",
    )
    runner.controller(
        "Global shard status includes test cluster",
        "ListShards",
        expected_text=sc,
    )
    runner.controller(
        "Add two shards in one command",
        "AddShard",
        sc,
        "2",
        expected_text="Successfully added 2 shard(s)",
        timeout=2400,
    )
    runner.controller(
        "Four-shard cluster reports online",
        "ListShards",
        sc,
        expected_text=f"{sc.lower()}-3",
    )
    runner.controller(
        "Create database on ShardedCluster",
        "AddDatabase",
        sc,
        db,
        expected_text=f"ShardedCluster '{sc}'",
        timeout=900,
    )
    runner.controller(
        "Block shard deletion while database exists",
        "DeleteShard",
        sc,
        "1",
        "--confirm",
        expect_success=False,
        expected_text="contains managed databases",
    )
    runner.controller(
        "Rotate ShardedCluster database credentials",
        "RotatePasswords",
        sc,
        db,
        expected_text="Rotated all three passwords",
        timeout=900,
    )
    runner.controller(
        "Disable ShardedCluster Owner",
        "DisableOwner",
        sc,
        db,
        "--confirm",
        expected_text="is now Disabled",
        timeout=900,
    )
    runner.controller(
        "Delete ShardedCluster database",
        "DeleteDatabase",
        sc,
        db,
        "--confirm",
        expected_text="was successfully deleted",
        timeout=900,
    )
    runner.controller(
        "Delete three shards in one command and leave one",
        "DeleteShard",
        sc,
        "3",
        "--confirm",
        expected_text="Total shards:    1",
        timeout=2400,
    )
    runner.controller(
        "Block deletion of final shard",
        "DeleteShard",
        sc,
        "1",
        "--confirm",
        expect_success=False,
        expected_text="must retain at least 1 shard",
    )
    runner.controller(
        "Delete temporary ShardedCluster",
        "DeleteShardedCluster",
        sc,
        "--confirm",
        expected_text=f"ShardedCluster '{sc}' was successfully deleted",
        timeout=2400,
    )
