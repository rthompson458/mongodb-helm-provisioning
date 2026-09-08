from __future__ import annotations

import uuid
from typing import Any

from . import kube
from .common import ControllerError
from .logging_component import log_event
from .terraform_runner import apply_inventory

LOCK_PREFIX = "tc-topology-lock-"


def lock_name(deployment_key: str) -> str:
    return f"{LOCK_PREFIX}{deployment_key}"


def read_topology_lock(
    config: dict[str, Any], deployment_key: str
) -> dict[str, Any] | None:
    obj = kube.get_json(config, "configmap", lock_name(deployment_key))
    if not obj:
        return None
    data = obj.get("data", {})
    try:
        return {
            "operation_id": str(data["operation_id"]),
            "action": str(data["action"]),
            "start_shards": int(data["start_shards"]),
            "target_shards": int(data["target_shards"]),
            "started_at": str(data.get("started_at", "")),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ControllerError(
            f"Topology lock for ShardedCluster '{deployment_key}' is invalid. "
            f"Inspect ConfigMap '{lock_name(deployment_key)}'."
        ) from exc


def describe_topology_lock(lock: dict[str, Any]) -> str:
    return (
        f"{lock['action']} {lock['start_shards']} -> {lock['target_shards']}"
    )


def require_no_topology_change(
    config: dict[str, Any],
    deployment_key: str,
    deployment: dict[str, Any],
) -> None:
    if deployment.get("deployment_type", "ReplicaSet") != "ShardedCluster":
        return
    lock = read_topology_lock(config, deployment_key)
    if not lock:
        return
    raise ControllerError(
        f"ShardedCluster '{deployment['display_name']}' has a topology change in progress.\n"
        f"Operation: {lock['action']}\n"
        f"Shard change: {lock['start_shards']} -> {lock['target_shards']}\n"
        "Database, credential, topology, and reconcile changes are blocked on this "
        "ShardedCluster until the topology operation completes. "
        f"Use 'ListShards {deployment['display_name']}' to view progress."
    )


def acquire_topology_lock(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    deployment_key: str,
    deployment: dict[str, Any],
    action: str,
    start_shards: int,
    target_shards: int,
) -> dict[str, Any]:
    operation_id = uuid.uuid4().hex
    log_event(
        "topology_lock.acquire.requested",
        deployment=deployment["display_name"],
        topology_action=action,
        start_shards=start_shards,
        target_shards=target_shards,
        operation_id=operation_id,
    )
    apply_inventory(
        config,
        inventory,
        {
            "action": "acquire_topology_lock",
            "deployment": deployment_key,
            "deployment_type": "ShardedCluster",
            "database": "",
            "members": int(deployment["members_per_shard"]),
            "topology_action": action,
            "operation_id": operation_id,
            "start_shards": start_shards,
            "target_shards": target_shards,
        },
    )
    lock = read_topology_lock(config, deployment_key)
    if not lock or lock["operation_id"] != operation_id:
        raise ControllerError(
            f"Could not verify the topology lock for ShardedCluster "
            f"'{deployment['display_name']}'. No shard change was started."
        )
    log_event(
        "topology_lock.acquire.succeeded",
        deployment=deployment["display_name"],
        topology_action=action,
        operation_id=operation_id,
    )
    return lock


def release_topology_lock(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    deployment_key: str,
    deployment: dict[str, Any],
    lock: dict[str, Any],
) -> None:
    apply_inventory(
        config,
        inventory,
        {
            "action": "release_topology_lock",
            "deployment": deployment_key,
            "deployment_type": "ShardedCluster",
            "database": "",
            "members": int(deployment["members_per_shard"]),
            "topology_action": lock["action"],
            "operation_id": lock["operation_id"],
            "start_shards": int(lock["start_shards"]),
            "target_shards": int(lock["target_shards"]),
        },
    )
    remaining = read_topology_lock(config, deployment_key)
    if remaining is not None:
        raise ControllerError(
            f"Shard topology changed successfully, but the topology lock for "
            f"ShardedCluster '{deployment['display_name']}' was not released. "
            f"Inspect ConfigMap '{lock_name(deployment_key)}'."
        )
    log_event(
        "topology_lock.release.succeeded",
        deployment=deployment["display_name"],
        topology_action=lock["action"],
        operation_id=lock["operation_id"],
    )


def validate_resume(
    deployment: dict[str, Any],
    lock: dict[str, Any],
    action: str,
    requested_count: int,
) -> None:
    expected_count = abs(int(lock["target_shards"]) - int(lock["start_shards"]))
    if lock["action"] != action or expected_count != requested_count:
        raise ControllerError(
            f"ShardedCluster '{deployment['display_name']}' already has a topology "
            f"change in progress: {describe_topology_lock(lock)}. "
            f"To resume it, run the same operation with count {expected_count}. "
            f"Use 'ListShards {deployment['display_name']}' to view progress."
        )
