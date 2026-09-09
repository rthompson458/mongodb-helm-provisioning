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


MANAGED_BY_SELECTOR = "app.kubernetes.io/managed-by=terraformController"
DEPLOYMENT_LOCK_PREFIX = "tc-deployment-lock-"


def _kubernetes_names(items: list[dict[str, Any]]) -> list[str]:
    """Return stable Kubernetes resource names for administrator reporting."""

    names = [
        str(item.get("metadata", {}).get("name", "<unknown>"))
        for item in items
    ]
    return sorted(names)


def managed_resource_inventory(
    config: dict[str, Any],
    vault: VaultClient,
) -> dict[str, list[str]]:
    """Return the controller's deployment-level managed resource inventory.

    This function is intentionally read-only. It combines the Vault-backed
    desired deployment inventory with the Kubernetes resources used during the
    controller zero-state check: managed MongoDB custom resources, managed PVCs,
    managed PVs, and terraformController deployment-lock ConfigMaps.
    """

    inventory = vault.load_inventory()
    deployments = sorted(
        str(inventory[key].get("display_name", key))
        for key in inventory
    )

    mongodb_resources = kube.list_json(
        config,
        "mongodb",
        label_selector=MANAGED_BY_SELECTOR,
    )
    pvcs = kube.list_json(
        config,
        "pvc",
        label_selector=MANAGED_BY_SELECTOR,
    )
    pvs = kube.list_json(
        config,
        "pv",
        label_selector=MANAGED_BY_SELECTOR,
        namespaced=False,
    )
    configmaps = kube.list_json(config, "configmap")
    locks = [
        item
        for item in configmaps
        if str(item.get("metadata", {}).get("name", "")).startswith(
            DEPLOYMENT_LOCK_PREFIX
        )
    ]

    return {
        "managed_deployments": deployments,
        "mongodb_resources": _kubernetes_names(mongodb_resources),
        "pvcs": _kubernetes_names(pvcs),
        "pvs": _kubernetes_names(pvs),
        "deployment_locks": _kubernetes_names(locks),
    }


def list_managed_resources(
    config: dict[str, Any],
    vault: VaultClient,
) -> None:
    """Print a concise read-only administrator inventory and zero-state result."""

    resources = managed_resource_inventory(config, vault)
    labels = [
        ("Managed deployments", "managed_deployments"),
        ("MongoDB resources", "mongodb_resources"),
        ("PVCs", "pvcs"),
        ("PVs", "pvs"),
        ("Deployment locks", "deployment_locks"),
    ]
    clean = all(not resources[key] for _, key in labels)

    print("terraformController Managed Resource Inventory")
    print()
    for label, key in labels:
        print(f"{label + ':':<21} {len(resources[key])}")

    print()
    print(f"Status: {'CLEAN' if clean else 'ATTENTION REQUIRED'}")

    if clean:
        return

    for label, key in labels:
        names = resources[key]
        if not names:
            continue
        print()
        print(f"{label}:")
        for name in names:
            print(f"  {name}")


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



def recover_orphaned_resources(
    config: dict[str, Any],
    vault: VaultClient,
    confirmed: bool,
) -> None:
    """Finish Terraform cleanup after desired-state inventory is already empty.

    This is an exceptional recovery path for a failed deployment destroy that
    removed its Vault inventory before Terraform finished destroying all
    controller-managed Kubernetes/storage resources.

    Safety rules are intentionally strict:
    - explicit --confirm is required,
    - Vault inventory must already be completely empty,
    - Kubernetes must contain no terraformController-managed MongoDB CRs.

    Only after both independent checks prove there is no live managed deployment
    does Terraform receive an empty desired-state inventory so it can finish
    destroying any resources still recorded in the controller backend state.
    """

    if not confirmed:
        raise ControllerError(
            "RecoverOrphanedResources is destructive and requires '--confirm'. "
            "Example: terraformController.py RecoverOrphanedResources --confirm"
        )

    inventory = vault.load_inventory()
    if inventory:
        names = ", ".join(
            inventory[key]["display_name"] for key in sorted(inventory)
        )
        raise ControllerError(
            "RecoverOrphanedResources is allowed only when the Vault-backed "
            f"controller inventory is empty. Managed deployment(s) still exist: {names}."
        )

    live = kube.list_json(
        config,
        "mongodb",
        label_selector="app.kubernetes.io/managed-by=terraformController",
    )
    if live:
        names = ", ".join(
            str(item.get("metadata", {}).get("name", "<unknown>"))
            for item in live
        )
        raise ControllerError(
            "RecoverOrphanedResources refused because live "
            "terraformController-managed MongoDB resource(s) still exist: "
            f"{names}."
        )

    log_event("orphaned_resources.recovery.requested")
    print(
        "Vault inventory is empty and no live terraformController-managed "
        "MongoDB deployments exist."
    )
    print("Applying empty desired state through Terraform to finish orphan cleanup ...")

    apply_inventory(config, {})

    remaining = kube.list_json(
        config,
        "mongodb",
        label_selector="app.kubernetes.io/managed-by=terraformController",
    )
    if remaining:
        raise ControllerError(
            "Terraform cleanup completed, but a managed MongoDB resource is still present."
        )

    log_event("orphaned_resources.recovery.succeeded")
    print("\nOrphaned terraformController resources were successfully reconciled.")
    print("Managed deployments: 0")
    print("Status:              Clean")
