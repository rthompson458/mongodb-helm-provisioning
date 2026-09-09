"""Serialize mutating operations on one ShardedCluster.

Why this exists:
Kubernetes can still report a ShardedCluster as Running while Terraform is
preparing storage or changing shardCount.  A simple phase check therefore does
not prevent two shells from starting conflicting operations at the same time.

The lock is a Kubernetes ConfigMap, but Python NEVER creates or deletes it
directly.  Python requests acquire/release actions through Terraform, and
terraform-dbaas/scripts/lifecycle.sh performs the atomic kubectl create/delete.

Read-only commands do not take this lock.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from . import kube
from .common import ControllerError
from .logging_component import log_event
from .terraform_runner import apply_inventory

LOCK_PREFIX = "tc-deployment-lock-"


def lock_name(deployment_key: str) -> str:
    """Return the deterministic ConfigMap name used for one deployment lock."""
    return f"{LOCK_PREFIX}{deployment_key}"


def read_deployment_lock(
    config: dict[str, Any], deployment_key: str
) -> dict[str, Any] | None:
    """Read and validate the current lock ConfigMap, if one exists."""

    obj = kube.get_json(config, "configmap", lock_name(deployment_key))
    if not obj:
        return None
    data = obj.get("data", {})
    try:
        return {
            "operation_id": str(data["operation_id"]),
            "category": str(data["category"]),
            "action": str(data["action"]),
            "database": str(data.get("database", "")),
            "start_shards": int(data.get("start_shards", "0")),
            "target_shards": int(data.get("target_shards", "0")),
            "started_at": str(data.get("started_at", "")),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ControllerError(
            f"Deployment lock for ShardedCluster '{deployment_key}' is invalid. "
            f"Inspect ConfigMap '{lock_name(deployment_key)}'."
        ) from exc


def describe_deployment_lock(lock: dict[str, Any]) -> str:
    """Return a short description suitable for status/error messages."""
    if lock["category"] == "topology":
        return (
            f"{lock['action']} {lock['start_shards']} -> "
            f"{lock['target_shards']}"
        )
    if lock["database"]:
        return f"{lock['action']} {lock['database']}"
    return lock["action"]


def require_no_active_change(
    config: dict[str, Any],
    deployment_key: str,
    deployment: dict[str, Any],
) -> None:
    """Block a new SC mutation when another managed change owns the lock."""

    if deployment.get("deployment_type", "ReplicaSet") != "ShardedCluster":
        return
    lock = read_deployment_lock(config, deployment_key)
    if not lock:
        return
    raise ControllerError(
        f"ShardedCluster '{deployment['display_name']}' is busy with another "
        f"managed change.\nActive change: {describe_deployment_lock(lock)}\n"
        "No conflicting change was attempted. "
        f"Use 'ListShards {deployment['display_name']}' to view cluster status."
    )


def acquire_deployment_lock(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    deployment_key: str,
    deployment: dict[str, Any],
    *,
    category: str,
    action: str,
    database: str = "",
    start_shards: int = 0,
    target_shards: int = 0,
) -> dict[str, Any]:
    """Ask Terraform to atomically create and then verify the deployment lock.

    kubectl create is intentionally used by the lifecycle script.  It fails
    when another process already created the same ConfigMap, which gives us
    cross-process mutual exclusion.
    """

    operation_id = uuid.uuid4().hex
    log_event(
        "deployment_lock.acquire.requested",
        deployment=deployment["display_name"],
        lock_category=category,
        lock_action=action,
        database=database,
        start_shards=start_shards,
        target_shards=target_shards,
        operation_id=operation_id,
    )
    apply_inventory(
        config,
        inventory,
        {
            "action": "acquire_deployment_lock",
            "deployment": deployment_key,
            "deployment_type": "ShardedCluster",
            "database": database,
            "members": int(deployment["members_per_shard"]),
            "lock_category": category,
            "lock_action": action,
            "operation_id": operation_id,
            "start_shards": start_shards,
            "target_shards": target_shards,
        },
    )
    lock = read_deployment_lock(config, deployment_key)
    if not lock or lock["operation_id"] != operation_id:
        raise ControllerError(
            f"Could not verify the deployment lock for ShardedCluster "
            f"'{deployment['display_name']}'. No protected change was started."
        )
    log_event(
        "deployment_lock.acquire.succeeded",
        deployment=deployment["display_name"],
        lock_category=category,
        lock_action=action,
        operation_id=operation_id,
    )
    return lock


def release_deployment_lock(
    config: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    deployment_key: str,
    deployment: dict[str, Any],
    lock: dict[str, Any],
    *,
    targets: list[str] | None = None,
) -> None:
    """Ask Terraform to release only the lock owned by this operation ID."""

    apply_inventory(
        config,
        inventory,
        {
            "action": "release_deployment_lock",
            "deployment": deployment_key,
            "deployment_type": "ShardedCluster",
            "database": lock.get("database", ""),
            "members": int(deployment["members_per_shard"]),
            "lock_category": lock["category"],
            "lock_action": lock["action"],
            "operation_id": lock["operation_id"],
            "start_shards": int(lock["start_shards"]),
            "target_shards": int(lock["target_shards"]),
        },
        targets=targets,
    )
    remaining = read_deployment_lock(config, deployment_key)
    if remaining is not None:
        raise ControllerError(
            f"The managed change completed, but the deployment lock for "
            f"ShardedCluster '{deployment['display_name']}' was not released. "
            f"Inspect ConfigMap '{lock_name(deployment_key)}'."
        )
    log_event(
        "deployment_lock.release.succeeded",
        deployment=deployment["display_name"],
        lock_category=lock["category"],
        lock_action=lock["action"],
        operation_id=lock["operation_id"],
    )


def validate_topology_resume(
    deployment: dict[str, Any],
    lock: dict[str, Any],
    action: str,
    requested_count: int,
) -> None:
    """Allow resume only when command type and requested count match the lock."""

    expected_count = abs(int(lock["target_shards"]) - int(lock["start_shards"]))
    if (
        lock["category"] != "topology"
        or lock["action"] != action
        or expected_count != requested_count
    ):
        raise ControllerError(
            f"ShardedCluster '{deployment['display_name']}' is busy with: "
            f"{describe_deployment_lock(lock)}. "
            f"Use 'ListShards {deployment['display_name']}' to view progress."
        )


@contextmanager
def protected_database_change(
    config: dict[str, Any],
    vault: Any,
    inventory: dict[str, dict[str, Any]],
    deployment_key: str,
    deployment: dict[str, Any],
    action: str,
    database: str,
) -> Iterator[None]:
    """Protect one SC database/credential mutation with acquire/finally-release.

    The finally block is important: ordinary command failures should not leave
    a stale lock.  Release uses freshly reloaded Vault state so it does not
    accidentally re-apply an old inventory snapshot.
    """

    if deployment.get("deployment_type", "ReplicaSet") != "ShardedCluster":
        yield
        return

    shard_count = int(deployment["shard_count"])
    lock = acquire_deployment_lock(
        config,
        inventory,
        deployment_key,
        deployment,
        category="database",
        action=action,
        database=database,
        start_shards=shard_count,
        target_shards=shard_count,
    )
    try:
        yield
    finally:
        latest_inventory = vault.load_inventory()
        latest_deployment = latest_inventory.get(deployment_key, deployment)
        release_deployment_lock(
            config,
            latest_inventory,
            deployment_key,
            latest_deployment,
            lock,
        )
