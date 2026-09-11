"""Read-only deployment and ShardedCluster status presentation.

Lifecycle modules decide whether a change is safe. This module only reads Vault
inventory plus live Kubernetes state and formats customer-facing status output.
Keeping presentation separate makes it harder for a read-only command to grow
mutation side effects.
"""

from __future__ import annotations

from typing import Any

from . import kube
from .common import print_table
from .deployment_lock import describe_deployment_lock, read_deployment_lock
from .deployments import deployment_type_label, require_deployment
from .vault import VaultClient

def _deployment_row(
    config: dict[str, Any], key: str, item: dict[str, Any]
) -> tuple[str, ...]:
    """Build one summary-table row for a managed deployment."""

    dtype = deployment_type_label(item)
    if dtype == "ShardedCluster":
        topology = (
            f"{item['shard_count']} shards / "
            f"{item['members_per_shard']} members"
        )
    else:
        topology = f"{item['members']} members"
    return (
        item["display_name"],
        dtype,
        kube.phase(config, key),
        topology,
        item["version"],
        str(len(item["databases"])),
    )


def list_deployments(config: dict[str, Any], vault: VaultClient) -> None:
    """List all managed ReplicaSets and ShardedClusters."""
    inventory = vault.load_inventory()
    if not inventory:
        print("No privateWorkerReplacement-managed MongoDB deployments exist.")
        return
    rows = [
        _deployment_row(config, key, inventory[key])
        for key in sorted(inventory)
    ]
    print_table(
        ("DEPLOYMENT", "TYPE", "PHASE", "TOPOLOGY", "MONGODB", "DATABASES"),
        rows,
    )


def list_replica_sets(config: dict[str, Any], vault: VaultClient) -> None:
    """List only controller-managed ReplicaSet deployments."""

    inventory = vault.load_inventory()
    rows = [
        _deployment_row(config, key, inventory[key])
        for key in sorted(inventory)
        if deployment_type_label(inventory[key]) == "ReplicaSet"
    ]
    if not rows:
        print("No privateWorkerReplacement-managed ReplicaSets exist.")
        return
    print_table(
        ("REPLICA SET", "TYPE", "PHASE", "TOPOLOGY", "MONGODB", "DATABASES"),
        rows,
    )


def list_sharded_clusters(config: dict[str, Any], vault: VaultClient) -> None:
    """List only controller-managed ShardedCluster deployments."""

    inventory = vault.load_inventory()
    rows = [
        _deployment_row(config, key, inventory[key])
        for key in sorted(inventory)
        if deployment_type_label(inventory[key]) == "ShardedCluster"
    ]
    if not rows:
        print("No privateWorkerReplacement-managed ShardedClusters exist.")
        return
    print_table(
        (
            "SHARDED CLUSTER",
            "TYPE",
            "PHASE",
            "TOPOLOGY",
            "MONGODB",
            "DATABASES",
        ),
        rows,
    )


def _status_count(item: dict[str, Any], lock: dict[str, Any] | None) -> int:
    """Include transitional shard ordinals while an add/delete lock is active."""

    count = int(item["shard_count"])
    if not lock:
        return count
    return max(
        count,
        int(lock["start_shards"]),
        int(lock["target_shards"]),
    )


def _shard_status(
    config: dict[str, Any],
    key: str,
    item: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Read live topology and overlay user-friendly Creating/Removing states."""

    lock = read_deployment_lock(config, key)
    status = kube.sharded_cluster_status(
        config, key, _status_count(item, lock)
    )

    if lock:
        start = int(lock["start_shards"])
        target = int(lock["target_shards"])
        for index, shard in enumerate(status["shards"]):
            if (
                lock["action"] == "AddShard"
                and start <= index < target
                and shard["desired"] == 0
            ):
                shard["status"] = "Creating"
            elif lock["action"] == "DeleteShard" and target <= index < start:
                shard["status"] = (
                    "Removed" if shard["desired"] == 0 else "Removing"
                )
    return status, lock


def _print_shards(
    config: dict[str, Any], key: str, item: dict[str, Any]
) -> None:
    """Print shard/component readiness plus the active topology change, if any."""

    status, lock = _shard_status(config, key, item)
    rows = [
        (
            x["shard"],
            x["status"],
            str(x["ready"]),
            str(x["desired"]),
            str(x["updated"]),
        )
        for x in status["shards"]
    ]
    print_table(("SHARD", "STATUS", "READY", "DESIRED", "UPDATED"), rows)
    if lock:
        print(f"Active change:   {describe_deployment_lock(lock)}")
        if lock["started_at"]:
            print(f"Started:         {lock['started_at']}")
    else:
        print("Active change:   None")
    print(
        f"Config servers: {status['config_servers']['status']} "
        f"({status['config_servers']['ready']}/"
        f"{status['config_servers']['desired']})"
    )
    print(
        f"mongos:         {status['mongos']['status']} "
        f"({status['mongos']['ready']}/{status['mongos']['desired']})"
    )


def list_deployment(
    config: dict[str, Any], vault: VaultClient, name: str
) -> None:
    """Show one deployment, including detailed shard status when applicable."""

    inventory = vault.load_inventory()
    key, item = require_deployment(inventory, name)
    dtype = deployment_type_label(item)
    print(f"Deployment:      {item['display_name']}")
    print(f"Type:            {dtype}")
    print(f"K8s Resource:    {key}")
    print(f"Phase:           {kube.phase(config, key)}")
    print(f"MongoDB:         {item['version']}")
    print(f"Databases:       {len(item['databases'])}")
    if dtype == "ReplicaSet":
        print(f"Members:         {item['members']}")
    else:
        print(f"Shards:          {item['shard_count']}")
        print(f"Members/Shard:   {item['members_per_shard']}")
        print(f"mongos:          {item['mongos_count']}")
        print(f"Config Servers:  {item['config_server_count']}")
        print()
        _print_shards(config, key, item)


def list_replica_set(
    config: dict[str, Any], vault: VaultClient, name: str
) -> None:
    """Show one managed ReplicaSet."""

    inventory = vault.load_inventory()
    _, item = require_deployment(inventory, name, "ReplicaSet")
    list_deployment(config, vault, item["display_name"])


def list_sharded_cluster(
    config: dict[str, Any], vault: VaultClient, name: str
) -> None:
    """Show one managed ShardedCluster and its component readiness."""

    inventory = vault.load_inventory()
    _, item = require_deployment(inventory, name, "ShardedCluster")
    list_deployment(config, vault, item["display_name"])


def list_shards(
    config: dict[str, Any],
    vault: VaultClient,
    name: str | None = None,
) -> None:
    """List shard status globally or for one targeted ShardedCluster."""

    inventory = vault.load_inventory()

    if name:
        key, item = require_deployment(inventory, name, "ShardedCluster")
        print(f"ShardedCluster: {item['display_name']}")
        print(f"Phase:          {kube.phase(config, key)}")
        print()
        _print_shards(config, key, item)
        return

    clusters = [
        (key, inventory[key])
        for key in sorted(inventory)
        if deployment_type_label(inventory[key]) == "ShardedCluster"
    ]
    if not clusters:
        print("No privateWorkerReplacement-managed ShardedClusters exist.")
        return

    rows: list[tuple[str, ...]] = []
    for key, item in clusters:
        status, lock = _shard_status(config, key, item)
        change = describe_deployment_lock(lock) if lock else "-"
        for shard in status["shards"]:
            rows.append(
                (
                    item["display_name"],
                    shard["shard"],
                    shard["status"],
                    str(shard["ready"]),
                    str(shard["desired"]),
                    str(shard["updated"]),
                    change,
                )
            )

    print_table(
        (
            "CLUSTER",
            "SHARD",
            "STATUS",
            "READY",
            "DESIRED",
            "UPDATED",
            "ACTIVE CHANGE",
        ),
        rows,
    )
