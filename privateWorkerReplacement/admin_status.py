"""Administrator-only formatting for controller-managed resource inventory.

ListManagedResources answers two administrator questions:
- what DBaaS-managed resources currently exist; and
- whether any cross-plane leftovers or mismatches require attention.

Permanent controller/platform infrastructure is displayed for visibility but does
not make an otherwise empty DBaaS environment dirty.
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
        ("Deployment locks", "deployment_locks"),
        ("Ops Manager DBaaS projects", "ops_manager_projects"),
        ("Ops Manager group secrets", "ops_manager_group_secrets"),
        ("Ops Manager orphan projects", "ops_manager_orphans"),
        ("Orphan group secrets", "orphan_group_secrets"),
        ("Missing Ops Manager projects", "missing_ops_manager_projects"),
        (
            "Controller infrastructure ConfigMaps",
            "controller_infrastructure_configmaps",
        ),
        ("Terraform backend states", "terraform_states"),
        ("Ops Manager platform project", "ops_manager_platform_project"),
        (
            "Ops Manager platform group secrets",
            "ops_manager_platform_group_secrets",
        ),
    ]

    attention_keys = {
        "ops_manager_orphans",
        "orphan_group_secrets",
        "missing_ops_manager_projects",
    }

    infrastructure_keys = {
        "controller_infrastructure_configmaps",
        "terraform_states",
        "ops_manager_platform_project",
        "ops_manager_platform_group_secrets",
    }

    managed_keys = {
        key
        for _, key in labels
        if key not in attention_keys | infrastructure_keys
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
