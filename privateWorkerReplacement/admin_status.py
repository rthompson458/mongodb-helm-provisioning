"""Administrator-only formatting for controller-managed resource inventory.

ListManagedResources answers a simple inventory question: what controller-owned
objects currently exist? Having resources is normal while the service is in
use, so nonzero inventory must not be labeled as an error condition.
"""

from __future__ import annotations

from typing import Any

from .maintenance import managed_resource_inventory
from .vault import VaultClient


def list_managed_resources(
    config: dict[str, Any],
    vault: VaultClient,
) -> None:
    """Print the authoritative DBaaS managed-resource inventory."""

    resources = managed_resource_inventory(config, vault)

    labels = [
        ("Managed deployments", "managed_deployments"),
        ("ReplicaSets", "replica_sets"),
        ("ShardedClusters", "sharded_clusters"),
        ("Databases", "databases"),
        ("Managed accounts", "managed_accounts"),
        ("MongoDB resources", "mongodb_resources"),
        ("MongoDB users", "mongodb_users"),
        ("DBaaS PVCs", "pvcs"),
        ("DBaaS PVs", "pvs"),
        ("Controller Secrets", "controller_secrets"),
        ("Controller ConfigMaps", "controller_configmaps"),
        ("Terraform backend states", "terraform_states"),
        ("Deployment locks", "deployment_locks"),
        ("Ops Manager platform project", "ops_manager_platform_project"),
        ("Ops Manager DBaaS projects", "ops_manager_projects"),
        ("Ops Manager orphan projects", "ops_manager_orphans"),
        ("Missing Ops Manager projects", "missing_ops_manager_projects"),
    ]

    attention_keys = {
        "ops_manager_orphans",
        "orphan_group_secrets",
        "missing_ops_manager_projects",
    }

    platform_keys = {
        "ops_manager_platform_project",
    }

    managed_keys = {
        key
        for _, key in labels
        if key not in attention_keys | platform_keys
    }

    attention = any(resources[key] for key in attention_keys)
    managed_present = any(resources[key] for key in managed_keys)

    if attention:
        status = "ATTENTION REQUIRED"
    elif managed_present:
        status = "MANAGED RESOURCES PRESENT"
    else:
        status = "CLEAN"

    print("privateWorkerReplacement Managed Resource Inventory")
    print()
    label_width = max(len(label) + 1 for label, _ in labels)

    for label, key in labels:
        print(f"{label + ':':<{label_width}} {len(resources[key])}")

    print()
    print(f"Status: {status}")

    for label, key in labels:
        names = resources[key]
        if not names:
            continue

        print()
        print(f"{label}:")
        for name in names:
            print(f"  {name}")

