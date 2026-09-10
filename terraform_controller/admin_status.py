"""Administrator-only formatting for controller-managed resource inventory.

ListManagedResources answers a simple inventory question: what controller-owned
objects currently exist? Having resources is normal while the service is in
use, so nonzero inventory must not be labeled as an error condition.
"""

from __future__ import annotations

from typing import Any

from .maintenance import managed_resource_inventory
from .vault import VaultClient


def list_managed_resources(config: dict[str, Any], vault: VaultClient) -> None:
    """Print managed resource counts and a neutral zero/nonzero status."""

    resources = managed_resource_inventory(config, vault)
    labels = [
        ("Managed deployments", "managed_deployments"),
        ("MongoDB resources", "mongodb_resources"),
        ("MongoDB users", "mongodb_users"),
        ("PVCs (PersistentVolumeClaims)", "pvcs"),
        ("PVs (PersistentVolumes)", "pvs"),
        ("Deployment locks", "deployment_locks"),
    ]
    clean = all(not resources[key] for _, key in labels)

    print("terraformController Managed Resource Inventory")
    print()
    label_width = max(len(label) + 1 for label, _ in labels)
    for label, key in labels:
        print(f"{label + ':':<{label_width}} {len(resources[key])}")

    print()
    print(f"Status: {'CLEAN' if clean else 'MANAGED RESOURCES PRESENT'}")

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
