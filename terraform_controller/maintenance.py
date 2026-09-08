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
from .deployment_lock import describe_deployment_lock, read_deployment_lock
from .deployments import deployment_type_label
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
