"""ShardedCluster shard-count lifecycle orchestration.

This module owns topology mutations after a ShardedCluster already exists.
Adding and deleting shards is resume-safe and serialized by a deployment lock.

The workflow deliberately separates storage capacity from live MongoDB topology:
AddShard prepares storage before increasing shardCount, while DeleteShard lowers
shardCount and waits for removed workloads before deleting old storage.
"""

from __future__ import annotations

from typing import Any

from . import kube
from .common import ControllerError
from .deployment_lock import (
    acquire_deployment_lock,
    read_deployment_lock,
    release_deployment_lock,
    validate_topology_resume,
)
from .deployments import require_deployment, require_running
from .logging_component import log_event
from .terraform_runner import apply_inventory
from .vault import VaultClient

def _require_positive_shard_count(count: int) -> None:
    """Require AddShard/DeleteShard COUNT to be at least one."""

    if count < 1:
        raise ControllerError("Shard COUNT must be at least 1.")


def _resume_or_acquire_lock(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    key: str,
    item: dict[str, Any],
    action: str,
    count: int,
    target: int,
) -> dict[str, Any]:
    """Resume the matching topology change or create a new deployment lock."""

    existing = read_deployment_lock(config, key)
    if existing:
        validate_topology_resume(item, existing, action, count)
        print(
            f"Resuming {existing['action']} on ShardedCluster "
            f"'{item['display_name']}' ({existing['start_shards']} -> "
            f"{existing['target_shards']} shards) ..."
        )
        log_event(
            "shard.operation.resumed",
            deployment=item["display_name"],
            lock_action=existing["action"],
            start_shards=existing["start_shards"],
            target_shards=existing["target_shards"],
            operation_id=existing["operation_id"],
        )
        return existing

    require_running(config, key, item)
    return acquire_deployment_lock(
        config,
        inventory,
        key,
        item,
        category="topology",
        action=action,
        start_shards=int(item["shard_count"]),
        target_shards=target,
    )


def _raise_topology_failure(
    item: dict[str, Any],
    action: str,
    count: int,
    exc: ControllerError,
) -> None:
    """Preserve the lock and turn a partial topology failure into a safe retry."""

    raise ControllerError(
        f"{action} did not complete for ShardedCluster '{item['display_name']}'. "
        "The deployment lock remains in place to prevent conflicting changes. "
        f"Correct the reported problem, then rerun '{action} "
        f"{item['display_name']} {count}'"
        + (" --confirm" if action == "DeleteShard" else "")
        + f" to resume safely. Underlying error: {exc}"
    ) from exc


def add_shard(
    config: dict[str, Any], vault: VaultClient, name: str, count: int = 1
) -> None:
    """Add COUNT shards with a resume-safe, two-stage Terraform workflow.

    Stage 1 prepares all required persistent storage.  Stage 2 raises shardCount.
    A deployment lock prevents concurrent mutations until the target is Online.
    """

    _require_positive_shard_count(count)
    inventory = vault.load_inventory()
    key, item = require_deployment(inventory, name, "ShardedCluster")

    existing = read_deployment_lock(config, key)
    if existing:
        validate_topology_resume(item, existing, "AddShard", count)
        target = int(existing["target_shards"])
        start = int(existing["start_shards"])
        lock = existing
        print(
            f"Resuming AddShard on ShardedCluster '{item['display_name']}' "
            f"({start} -> {target} shards) ..."
        )
    else:
        start = int(item["shard_count"])
        target = start + count
        lock = _resume_or_acquire_lock(
            config, inventory, key, item, "AddShard", count, target
        )

    log_event(
        "shard.add.requested",
        deployment=item["display_name"],
        add_count=count,
        previous_shards=start,
        target_shards=target,
        operation_id=lock["operation_id"],
    )

    try:
        # Add capacity first. MongoDB must never be asked to create a shard for
        # which the static-local storage layer has not already been prepared.
        if int(item["storage_shard_count"]) < target:
            item["storage_shard_count"] = target
            apply_inventory(config, inventory)

        # Only after storage exists do we raise the desired live shard count.
        if int(item["shard_count"]) < target:
            item["shard_count"] = target
            apply_inventory(config, inventory)

        print(
            f"Waiting for ShardedCluster '{item['display_name']}' to reach "
            f"{target} fully online shards ..."
        )
        kube.wait_sharded_cluster_ready(
            config, key, target, config["sc_ready_timeout"]
        )

        release_deployment_lock(config, inventory, key, item, lock)
    except ControllerError as exc:
        _raise_topology_failure(item, "AddShard", count, exc)

    log_event(
        "shard.add.succeeded",
        deployment=item["display_name"],
        added=count,
        previous_shards=start,
        shard_count=target,
    )
    print(
        f"\nSuccessfully added {count} shard(s) to ShardedCluster "
        f"'{item['display_name']}'."
    )
    print(f"Previous shards: {start}")
    print(f"Total shards:    {target}")
    print("Status:          Running")


def delete_shard(
    config: dict[str, Any],
    vault: VaultClient,
    name: str,
    count: int = 1,
    confirmed: bool = False,
) -> None:
    """Delete COUNT highest-numbered shards while always retaining at least one.

    Shard removal is allowed while application databases exist. Python owns
    validation, locking, waiting, and reporting only. The topology mutation is
    still Terraform-driven: Terraform lowers the MongoDB ShardedCluster
    spec.shardCount and the MongoDB Kubernetes Operator/Ops Manager reconciles
    the supported scale-down before Terraform removes old storage.
    """

    _require_positive_shard_count(count)
    if not confirmed:
        raise ControllerError(
            "DeleteShard is destructive and requires '--confirm'. "
            f"Example: privateWorkerReplacement.py DeleteShard {name} {count} --confirm"
        )

    inventory = vault.load_inventory()
    key, item = require_deployment(inventory, name, "ShardedCluster")
    existing = read_deployment_lock(config, key)

    if existing:
        validate_topology_resume(item, existing, "DeleteShard", count)
        start = int(existing["start_shards"])
        target = int(existing["target_shards"])
        lock = existing
        print(
            f"Resuming DeleteShard on ShardedCluster '{item['display_name']}' "
            f"({start} -> {target} shards) ..."
        )
    else:
        start = int(item["shard_count"])
        target = start - count
        if target < 1:
            maximum = max(0, start - 1)
            raise ControllerError(
                f"Cannot delete {count} shard(s) from ShardedCluster "
                f"'{item['display_name']}'. It currently has {start} shard(s), "
                "and a ShardedCluster must retain at least 1 shard. "
                f"Maximum deletable now: {maximum}."
            )

        require_running(config, key, item)

        lock = acquire_deployment_lock(
            config,
            inventory,
            key,
            item,
            category="topology",
            action="DeleteShard",
            start_shards=start,
            target_shards=target,
        )

    log_event(
        "shard.delete.requested",
        deployment=item["display_name"],
        delete_count=count,
        previous_shards=start,
        target_shards=target,
        operation_id=lock["operation_id"],
    )

    try:
        # Lower MongoDB topology first. Storage stays intact until the Operator
        # has removed the old shard workloads and the surviving topology is healthy.
        if int(item["shard_count"]) > target:
            print(
                f"Scaling ShardedCluster '{item['display_name']}' from "
                f"{start} to {target} shard(s) through Terraform ..."
            )
            if item["databases"]:
                print(
                    "Existing databases will be preserved while the MongoDB "
                    "Kubernetes Operator/Ops Manager reconciles the shard scale-down."
                )
            item["shard_count"] = target
            apply_inventory(config, inventory)

        kube.wait_sharded_cluster_ready(
            config, key, target, config["sc_ready_timeout"]
        )
        for index in range(target, start):
            kube.wait_absent(
                config,
                "statefulset",
                f"{key}-{index}",
                config["sc_ready_timeout"],
            )

        # Now that the removed StatefulSets are gone, Terraform may safely
        # remove the no-longer-needed shard storage.
        if int(item["storage_shard_count"]) > target:
            item["storage_shard_count"] = target
            apply_inventory(config, inventory)

        release_deployment_lock(config, inventory, key, item, lock)
    except ControllerError as exc:
        _raise_topology_failure(item, "DeleteShard", count, exc)

    log_event(
        "shard.delete.succeeded",
        deployment=item["display_name"],
        deleted=count,
        previous_shards=start,
        shard_count=target,
    )
    print(
        f"\nSuccessfully deleted {count} shard(s) from ShardedCluster "
        f"'{item['display_name']}'."
    )
    print(f"Previous shards: {start}")
    print(f"Total shards:    {target}")
    print("Status:          Running")
    if item["databases"]:
        print(f"Databases:       {len(item['databases'])} preserved")
