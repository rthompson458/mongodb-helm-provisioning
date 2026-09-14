"""End-to-end ShardedCluster and multi-shard lifecycle scenario.

Canonical full-suite test intent:
17. Reject ShardedCluster creation above the configured shard maximum.
18. Create the initial temporary ShardedCluster and wait for every component.
19. Verify targeted shard status reports the expected shard.
20. Verify global shard status includes the temporary cluster.
21. Add the allowed number of shards without exceeding the configured maximum.
22. Verify the expanded topology is online.
23. Reject an AddShard request whose resulting topology exceeds the maximum.
24. Create a managed database and accounts on the ShardedCluster.
25. Remove one shard while the database exists, preserving supported service.
26. Rotate all three ShardedCluster database credentials.
27. Disable the database Owner account.
28. Re-enable the database Owner account.
29. Delete the ShardedCluster database and managed accounts.
30. Reduce the cluster to one remaining shard.
31. Prove the final shard cannot be deleted.
32. Delete the temporary ShardedCluster and complete cleanup.
"""

# MAINTAINER READING GUIDE
# End-to-end ShardedCluster lifecycle scenario, including topology, database/account work, readiness, and cleanup.
# Treat these tests as executable design documentation. A failing assertion
# should identify which controller contract changed, not merely that text moved.


from __future__ import annotations

from privateWorkerReplacement.config import load_config

from .runner import HarnessRunner

TEST_COUNT = 16


# Read in lifecycle order: create cluster -> wait for all components -> topology
# changes -> database/account checks -> cleanup.
def run(runner: HarnessRunner) -> None:
    """Exercise count-based shard operations, stopping on prerequisite failure."""

    ctx = runner.context
    sc = ctx.sharded_cluster
    db = ctx.database
    maximum_shards = int(load_config(ctx.config_path)["max_shards_per_cluster"])
    initial_shards = min(3, maximum_shards)

    # TEST 17 - Reject ShardedCluster creation above the configured shard maximum.
    # WHY: Enforces the service limit before Terraform can create an unsupported topology.
    # PASS: AddShardedCluster is rejected with the configured maximum in the error.
    blocked_create = runner.controller(
        "Block ShardedCluster creation above configured maximum",
        "AddShardedCluster",
        f"{sc}-over-limit",
        "--shards",
        str(maximum_shards + 1),
        expect_success=False,
        expected_text=f"configured maximum of {maximum_shards}",
        timeout=300,
    )
    if not blocked_create.passed:
        return

    # TEST 18 - Create the temporary ShardedCluster.
    # WHY: Proves mongos, config servers, shards, storage, and controller setup can converge together.
    # PASS: The asynchronous AddShardedCluster operation completes successfully.
    created = runner.controller_async(
        f"Create {initial_shards}-shard test cluster",
        "AddShardedCluster",
        sc,
        "--shards",
        str(initial_shards),
        timeout=2400,
    )
    if not created.passed:
        return

    # TEST 19 - Read shard status for one ShardedCluster.
    # WHY: Proves the targeted status path can identify the expected shard members.
    # PASS: ListShards for the test cluster includes the expected shard name.
    targeted_status = runner.controller(
        "Targeted shard status works",
        "ListShards",
        sc,
        expected_text=f"{sc.lower()}-{initial_shards - 1}",
    )
    if not targeted_status.passed:
        return

    # TEST 20 - Read global shard status after cluster creation.
    # WHY: Proves the all-cluster view discovers the newly managed ShardedCluster.
    # PASS: Global ListShards output includes the test cluster.
    global_status = runner.controller(
        "Global shard status includes test cluster",
        "ListShards",
        expected_text=sc,
    )
    if not global_status.passed:
        return

    # TEST 21 - Add shards without exceeding the configured maximum.
    # WHY: Proves supported scale-out works while respecting the service shard ceiling.
    # PASS: AddShard succeeds, or the test records that the cluster already started at the maximum.
    add_count = min(2, maximum_shards - initial_shards)
    if add_count > 0:
        added = runner.controller_async(
            f"Add {add_count} shard(s) without exceeding configured maximum",
            "AddShard",
            sc,
            str(add_count),
            timeout=2400,
        )
    else:
        added = runner.check(
            "Configured maximum leaves no room for allowed shard expansion",
            True,
            note=(
                f"Cluster started at the configured maximum of {maximum_shards}; "
                "no successful AddShard request is possible."
            ),
        )
    if not added.passed:
        return

    # TEST 22 - Verify the expanded ShardedCluster is online.
    # WHY: A successful AddShard request is not enough; the resulting topology must be usable.
    # PASS: ListShards reports the highest expected shard after expansion.
    current_shards = initial_shards + add_count
    expanded_status = runner.controller(
        "Expanded cluster reports online",
        "ListShards",
        sc,
        expected_text=f"{sc.lower()}-{current_shards - 1}",
    )
    if not expanded_status.passed:
        return

    # TEST 23 - Reject an AddShard request that would exceed the maximum.
    # WHY: Prevents an existing cluster from being scaled past its configured service limit.
    # PASS: AddShard is rejected before an unsupported target topology is applied.
    blocked_add_count = maximum_shards - current_shards + 1
    blocked_add = runner.controller_async(
        "Block AddShard target above configured maximum",
        "AddShard",
        sc,
        str(blocked_add_count),
        expect_success=False,
        expected_text=f"configured maximum of {maximum_shards}",
        timeout=300,
    )
    if not blocked_add.passed:
        return

    # TEST 24 - Create a database on the ShardedCluster.
    # WHY: Proves normal database service works on a fully ready sharded deployment.
    # PASS: The asynchronous AddDatabase operation completes successfully.
    # Database create/delete are customer-asynchronous just like the surrounding
    # deployment/topology work. Polling here proves the lifecycle finished, not
    # merely that the public request was accepted.
    created_db = runner.controller_async(
        "Create database on ShardedCluster",
        "AddDatabase",
        sc,
        db,
        timeout=1200,
    )
    if not created_db.passed:
        return

    # TEST 25 - Remove one shard while a managed database exists.
    # WHY: Proves supported shard contraction preserves database service instead of requiring an empty cluster.
    # PASS: DeleteShard succeeds, or the test records that only one shard was available.
    if current_shards > 1:
        deleted_one = runner.controller_async(
            "Delete a shard while database exists",
            "DeleteShard",
            sc,
            "1",
            "--confirm",
            timeout=2400,
        )
        current_shards -= 1
    else:
        deleted_one = runner.check(
            "One-shard maximum leaves no shard to delete while database exists",
            True,
            note="Final-shard protection is tested separately.",
        )
    if not deleted_one.passed:
        return

    # TEST 26 - Rotate all ShardedCluster database credentials.
    # WHY: Proves credential rotation works through the sharded deployment path.
    # PASS: RotatePasswords reports that all three passwords were rotated.
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

    # TEST 27 - Disable the ShardedCluster database Owner account.
    # WHY: Proves Owner access can be disabled without removing the database or other accounts.
    # PASS: DisableOwner reports the Owner account as Disabled.
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

    # TEST 28 - Re-enable the ShardedCluster database Owner account.
    # WHY: Proves Owner access can be restored through the supported workflow.
    # PASS: EnableOwner reports the Owner account as Enabled.
    enabled = runner.controller(
        "Re-enable ShardedCluster Owner",
        "EnableOwner",
        sc,
        db,
        expected_text="is now Enabled",
        timeout=900,
    )
    if not enabled.passed:
        return

    # TEST 29 - Delete the managed ShardedCluster database.
    # WHY: Proves database teardown and account cleanup work on a sharded deployment.
    # PASS: The asynchronous DeleteDatabase operation completes successfully.
    deleted_db = runner.controller_async(
        "Delete ShardedCluster database",
        "DeleteDatabase",
        sc,
        db,
        "--confirm",
        timeout=1200,
    )
    if not deleted_db.passed:
        return

    # TEST 30 - Reduce the ShardedCluster to one remaining shard.
    # WHY: Prepares the minimum-topology boundary and proves multi-shard contraction can complete.
    # PASS: DeleteShard leaves exactly one shard, or no action is needed because one already remains.
    remaining_to_delete = current_shards - 1
    if remaining_to_delete > 0:
        reduced = runner.controller_async(
            f"Delete {remaining_to_delete} shard(s) and leave one",
            "DeleteShard",
            sc,
            str(remaining_to_delete),
            "--confirm",
            timeout=2400,
        )
    else:
        reduced = runner.check(
            "Cluster already has one shard after database lifecycle",
            True,
            note="No additional contraction is required before final-shard protection.",
        )
    if not reduced.passed:
        return

    # TEST 31 - Refuse deletion of the final shard.
    # WHY: A ShardedCluster cannot remain valid with zero shards.
    # PASS: DeleteShard is rejected because at least one shard must remain.
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

    # TEST 32 - Delete the temporary ShardedCluster.
    # WHY: Proves complete sharded deployment teardown succeeds after database cleanup.
    # PASS: The asynchronous DeleteShardedCluster operation completes successfully.
    runner.controller_async(
        "Delete temporary ShardedCluster",
        "DeleteShardedCluster",
        sc,
        "--confirm",
        timeout=2400,
    )
