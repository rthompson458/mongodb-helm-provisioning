"""ShardedCluster shard-topology mutation orchestration.

This module owns AddShard/DeleteShard behavior:
- validate requested shard counts;
- acquire or safely resume the ShardedCluster topology lock;
- stage static-local storage before scale-up;
- change desired shardCount through Terraform;
- wait for the target topology to become fully healthy;
- wait for removed shard workloads before storage cleanup; and
- retain the deployment lock when a topology operation fails so the same
  request can be resumed safely.

Deployment creation/deletion and shared readiness checks live in deployments.py.
Read-only shard presentation lives in deployment_status.py. Python does not
directly edit the MongoDB custom resource or delete shard storage.
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
    """Reject zero/negative shard counts before any topology mutation begins."""

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
    """Resume a matching topology operation or acquire a new deployment lock."""

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
    """Explain how to resume a failed shard operation without clearing its lock."""

    raise ControllerError(
        f"{action} did not complete for ShardedCluster '{item['display_name']}'. "
        "The deployment lock remains in place to prevent conflicting changes. "
        f"Correct the reported problem, then rerun '{action} "
        f"{item['display_name']} {count}'"
        + (" --confirm" if action == "DeleteShard" else "")
        + f" to resume safely. Underlying error: {exc}"
    ) from exc


def add_shard(
    config: dict[str, Any],
    vault: VaultClient,
    name: str,
    count: int = 1,
) -> None:
    """Add COUNT shards with a resume-safe, two-stage Terraform workflow.

    Stage 1 prepares all required persistent storage. Stage 2 raises shardCount.
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
            config,
            inventory,
            key,
            item,
            "AddShard",
            count,
            target,
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
        # Static-local storage must exist before the MongoDB CR asks the
        # Operator to schedule the new shard StatefulSets.
        if int(item["storage_shard_count"]) < target:
            item["storage_shard_count"] = target
            apply_inventory(config, inventory)

        if int(item["shard_count"]) < target:
            item["shard_count"] = target
            apply_inventory(config, inventory)

        print(
            f"Waiting for ShardedCluster '{item['display_name']}' to reach "
            f"{target} fully online shards ..."
        )
        kube.wait_sharded_cluster_ready(
            config,
            key,
            target,
            config["sc_ready_timeout"],
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
    validation, locking, waiting, and reporting only. Terraform lowers the
    MongoDB ShardedCluster ``spec.shardCount`` and the MongoDB Kubernetes
    Operator/Ops Manager performs the supported scale-down before Terraform
    removes old storage.
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
            config,
            key,
            target,
            config["sc_ready_timeout"],
        )

        # Wait for removed StatefulSets to disappear before lowering the storage
        # count. That keeps storage cleanup from racing terminating shard pods.
        for index in range(target, start):
            kube.wait_absent(
                config,
                "statefulset",
                f"{key}-{index}",
                config["sc_ready_timeout"],
            )

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
