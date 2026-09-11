"""Administrator-only formatting for controller-managed resource inventory.

ListManagedResources answers three administrator questions:
- what DBaaS-managed resources currently exist;
- whether any cross-plane leftovers or mismatches require attention; and
- which deployment owns the major Kubernetes storage/runtime resources.

The default view is intentionally operational and compact. --verbose adds full
object-name inventories for forensic troubleshooting.

Permanent controller/platform infrastructure is displayed for visibility but
does not make an otherwise empty DBaaS environment dirty. Missing permanent
infrastructure is different: that is an actionable mismatch and must raise
ATTENTION REQUIRED.
"""

from __future__ import annotations

import textwrap
from typing import Any

from .common import normalize_deployment, print_table
from .maintenance import managed_resource_inventory
from .vault import VaultClient


def _print_summary_section(
    title: str,
    rows: list[tuple[str, str]],
) -> None:
    """Print one compact count/status section with aligned values."""

    print(title)
    print("-" * len(title))
    width = max(len(label) + 1 for label, _ in rows)
    for label, value in rows:
        print(f"{label + ':':<{width}} {value}")


def _deployment_rows(
    resources: dict[str, list[str]],
) -> list[tuple[str, ...]]:
    """Build one compact ownership/status row for each managed deployment."""

    replica_sets = {name.lower() for name in resources["replica_sets"]}
    sharded_clusters = {name.lower() for name in resources["sharded_clusters"]}
    mongodb_resources = {
        name.lower(): name for name in resources["mongodb_resources"]
    }

    rows: list[tuple[str, ...]] = []
    for display in resources["managed_deployments"]:
        key, _ = normalize_deployment(display)
        lowered = display.lower()

        if lowered in replica_sets:
            deployment_type = "ReplicaSet"
        elif lowered in sharded_clusters:
            deployment_type = "ShardedCluster"
        else:
            deployment_type = "Unknown"

        mongodb_resource = mongodb_resources.get(key, "MISSING")
        pvc_count = sum(
            name.startswith(f"data-{key}-")
            for name in resources["pvcs"]
        )
        pv_count = sum(
            name.startswith(f"{key}-")
            for name in resources["pvs"]
        )
        database_count = sum(
            value.split("/", 1)[0].lower() == lowered
            for value in resources["databases"]
            if "/" in value
        )

        rows.append(
            (
                display,
                deployment_type,
                mongodb_resource,
                str(pvc_count),
                str(pv_count),
                str(database_count),
            )
        )

    return rows


def _print_verbose_details(
    labels: list[tuple[str, str]],
    resources: dict[str, list[str]],
) -> None:
    """Print full object-name inventories for non-empty categories."""

    print("Full Resource Details")
    print("---------------------")
    for label, key in labels:
        names = resources[key]
        if not names:
            continue

        prefix = f"{label}: "
        lines = textwrap.wrap(
            prefix + ", ".join(names),
            width=100,
            subsequent_indent=" " * len(prefix),
            break_long_words=False,
            break_on_hyphens=False,
        )
        for line in lines:
            print(line)


def list_managed_resources(
    config: dict[str, Any],
    vault: VaultClient,
    verbose: bool = False,
) -> None:
    """Print the authoritative DBaaS inventory in compact or verbose form."""

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
            "Missing Ops Manager platform project",
            "missing_ops_manager_platform_project",
        ),
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
        "missing_ops_manager_platform_project",
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
    print(f"Status: {status}")
    print()

    _print_summary_section(
        "Managed Resources",
        [
            ("Deployments", str(len(resources["managed_deployments"]))),
            ("  ReplicaSets", str(len(resources["replica_sets"]))),
            ("  ShardedClusters", str(len(resources["sharded_clusters"]))),
            ("Databases", str(len(resources["databases"]))),
            ("Managed accounts", str(len(resources["managed_accounts"]))),
            ("MongoDB resources", str(len(resources["mongodb_resources"]))),
            ("MongoDB users", str(len(resources["mongodb_users"]))),
            (
                "DBaaS PVCs / PVs",
                f"{len(resources['pvcs'])} / {len(resources['pvs'])}",
            ),
        ],
    )
    print()

    _print_summary_section(
        "Platform Resources",
        [
            ("Controller Secrets", str(len(resources["controller_secrets"]))),
            ("Controller ConfigMaps", str(len(resources["controller_configmaps"]))),
            ("Deployment locks", str(len(resources["deployment_locks"]))),
            (
                "Ops Manager DBaaS projects",
                str(len(resources["ops_manager_projects"])),
            ),
            (
                "Ops Manager group secrets",
                str(len(resources["ops_manager_group_secrets"])),
            ),
            (
                "Controller infrastructure ConfigMaps",
                str(len(resources["controller_infrastructure_configmaps"])),
            ),
            ("Terraform backend states", str(len(resources["terraform_states"]))),
            (
                "Ops Manager platform project",
                str(len(resources["ops_manager_platform_project"])),
            ),
            (
                "Ops Manager platform group secrets",
                str(len(resources["ops_manager_platform_group_secrets"])),
            ),
        ],
    )
    print()

    _print_summary_section(
        "Health / Consistency",
        [
            (
                "Ops Manager orphan projects",
                str(len(resources["ops_manager_orphans"])),
            ),
            (
                "Orphan group secrets",
                str(len(resources["orphan_group_secrets"])),
            ),
            (
                "Missing Ops Manager projects",
                str(len(resources["missing_ops_manager_projects"])),
            ),
            (
                "Missing Ops Manager platform project",
                str(len(resources["missing_ops_manager_platform_project"])),
            ),
        ],
    )

    rows = _deployment_rows(resources)
    if rows:
        print()
        print("Deployment Details")
        print("------------------")
        print_table(
            ("DEPLOYMENT", "TYPE", "MONGODB RESOURCE", "PVCS", "PVS", "DATABASES"),
            rows,
        )

    if verbose:
        print()
        _print_verbose_details(labels, resources)
