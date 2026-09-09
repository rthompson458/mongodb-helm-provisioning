"""Reconcile all Vault-backed desired state through Terraform.

Reconcile is the repair/convergence command.  It does not invent new desired
state; it reloads the state already recorded in Vault, asks Terraform to apply
it, then verifies Kubernetes resources converge.

A live ShardedCluster mutation lock blocks Reconcile because a broad Terraform
apply must not race with an AddShard/DeleteShard/database operation.
"""

from __future__ import annotations

from typing import Any

from . import kube
from .common import ControllerError, account_resource_name
from .deployment_lock import (
    describe_deployment_lock,
    read_deployment_lock,
    release_deployment_lock,
)
from .deployments import deployment_type_label, require_deployment
from .logging_component import log_event
from .terraform_runner import apply_inventory
from .vault import VaultClient


def reconcile(config: dict[str, Any], vault: VaultClient) -> None:
    """Reapply complete Vault-backed desired state and verify convergence."""
    inventory = vault.load_inventory()
    if not inventory:
        print("No terraformController-managed MongoDB deployments exist. Nothing to reconcile.")
        return

    # Reconcile touches the complete inventory.  Refuse to start while any
    # ShardedCluster has a protected mutation in flight.
    active = []
    for key in sorted(inventory):
        deployment = inventory[key]
        if deployment_type_label(deployment) != "ShardedCluster":
            continue
        lock = read_deployment_lock(config, key)
        if lock:
            active.append(
                f"  {deployment['display_name']}: {describe_deployment_lock(lock)}"
            )

    if active:
        raise ControllerError(
            "Reconcile is blocked while a ShardedCluster managed change is in progress:\n"
            + "\n".join(active)
            + "\nUse ListShards [SHARDED_CLUSTER] to view progress."
        )

    log_event("reconcile.requested", deployments=len(inventory))
    # From this point forward Terraform owns the mutation.  Python only waits
    # for the resulting Kubernetes state and reports it.
    apply_inventory(config, inventory)
    print("\nWaiting for managed deployments and accounts to converge ...")

    for key in sorted(inventory):
        deployment = inventory[key]
        dtype = deployment_type_label(deployment)
        timeout = (
            config["sc_ready_timeout"]
            if dtype == "ShardedCluster"
            else config["rs_ready_timeout"]
        )

        if dtype == "ShardedCluster":
            kube.wait_sharded_cluster_ready(
                config, key, int(deployment["shard_count"]), timeout
            )
        else:
            kube.wait_phase(config, "mongodb", key, "Running", timeout)

        kube.wait_phase(
            config,
            "mongodbuser",
            kube.controller_user(key),
            "Updated",
            timeout,
        )

        for db_key in sorted(deployment["databases"]):
            db = deployment["databases"][db_key]
            if db["owner_disabled"]:
                kube.wait_absent(
                    config,
                    "mongodbuser",
                    account_resource_name(key, db_key, "owner"),
                    timeout,
                )
                accounts = ("readwrite", "read")
            else:
                accounts = ("owner", "readwrite", "read")

            for account in accounts:
                kube.wait_phase(
                    config,
                    "mongodbuser",
                    account_resource_name(key, db_key, account),
                    "Updated",
                    timeout,
                )

        print(f"  {deployment['display_name']} ({dtype}): Running")

    log_event("reconcile.succeeded", deployments=len(inventory))
    print("\nReconcile complete.")



def recover_deployment_lock(
    config: dict[str, Any],
    vault: VaultClient,
    name: str,
    confirmed: bool,
) -> None:
    """Release a completed topology lock without reconciling unrelated storage.

    This recovery path is intentionally narrow. It is for an interrupted
    AddShard/DeleteShard where MongoDB has already reached the lock's target
    topology but a later bookkeeping/storage step failed.

    The lock release itself is still Terraform-driven. A targeted Terraform
    apply executes only terraform_data.lifecycle_operation so unrelated legacy
    storage resources are not reconciled during recovery.
    """

    if not confirmed:
        raise ControllerError(
            "RecoverDeploymentLock is a recovery action and requires '--confirm'. "
            f"Example: terraformController.py RecoverDeploymentLock {name} --confirm"
        )

    inventory = vault.load_inventory()
    key, deployment = require_deployment(inventory, name, "ShardedCluster")
    lock = read_deployment_lock(config, key)
    if not lock:
        raise ControllerError(
            f"ShardedCluster '{deployment['display_name']}' has no active deployment lock."
        )
    if lock["category"] != "topology" or lock["action"] not in {"AddShard", "DeleteShard"}:
        raise ControllerError(
            f"RecoverDeploymentLock only supports topology locks. Active change: "
            f"{describe_deployment_lock(lock)}."
        )

    target = int(lock["target_shards"])
    recorded = int(deployment["shard_count"])
    if recorded != target:
        raise ControllerError(
            f"Cannot recover lock for ShardedCluster '{deployment['display_name']}'. "
            f"Vault desired shard count is {recorded}, but the lock target is {target}."
        )

    mongodb = kube.get_json(config, "mongodb", key)
    if not mongodb:
        raise ControllerError(
            f"Cannot recover lock for ShardedCluster '{deployment['display_name']}' "
            "because the MongoDB resource is absent."
        )
    live_target = int(mongodb.get("spec", {}).get("shardCount", 0) or 0)
    if live_target != target:
        raise ControllerError(
            f"Cannot recover lock for ShardedCluster '{deployment['display_name']}'. "
            f"Live MongoDB shardCount is {live_target}, but the lock target is {target}."
        )

    status = kube.sharded_cluster_status(config, key, target)
    shards_ready = all(x["status"] == "Online" for x in status["shards"])
    config_ready = status["config_servers"]["status"] == "Online"
    mongos_ready = status["mongos"]["status"] == "Online"
    if not (
        status["phase"] == "Running"
        and shards_ready
        and config_ready
        and mongos_ready
    ):
        raise ControllerError(
            f"Cannot recover lock for ShardedCluster '{deployment['display_name']}'. "
            "The target topology is not fully healthy."
        )

    if lock["action"] == "DeleteShard":
        for index in range(target, int(lock["start_shards"])):
            if kube.get_json(config, "statefulset", f"{key}-{index}") is not None:
                raise ControllerError(
                    f"Cannot recover lock for ShardedCluster '{deployment['display_name']}'. "
                    f"Removed shard StatefulSet '{key}-{index}' still exists."
                )

    log_event(
        "deployment_lock.recovery.requested",
        deployment=deployment["display_name"],
        lock_action=lock["action"],
        start_shards=int(lock["start_shards"]),
        target_shards=target,
        operation_id=lock["operation_id"],
    )

    release_deployment_lock(
        config,
        inventory,
        key,
        deployment,
        lock,
        targets=["terraform_data.lifecycle_operation"],
    )

    log_event(
        "deployment_lock.recovery.succeeded",
        deployment=deployment["display_name"],
        lock_action=lock["action"],
        operation_id=lock["operation_id"],
    )
    print(
        f"Recovered completed {lock['action']} lock for ShardedCluster "
        f"'{deployment['display_name']}'."
    )
    print(f"Current shards: {target}")
    print("Status:         Running")
    print("Deployment lock: Released")
