from __future__ import annotations

from typing import Any

from . import kube
from .common import account_resource_name
from .deployments import deployment_type_label
from .logging_component import log_event
from .terraform_runner import apply_inventory
from .vault import VaultClient


def reconcile(config: dict[str, Any], vault: VaultClient) -> None:
    """Reapply complete Vault-backed desired state through Terraform."""
    inventory = vault.load_inventory()
    if not inventory:
        print("No terraformController-managed MongoDB deployments exist. Nothing to reconcile.")
        return

    log_event("reconcile.requested", deployments=len(inventory))
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
