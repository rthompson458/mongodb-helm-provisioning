"""Shared test fixtures for privateWorkerReplacement unit tests.

This module intentionally contains test data builders only.  Keeping common
fixtures here prevents every test file from carrying a large copy of the same
ReplicaSet/ShardedCluster/Vault dictionaries.
"""

from __future__ import annotations

from typing import Any


def deployment_inventory(
    *,
    deployment_type: str = "ReplicaSet",
    name: str = "RS1",
    with_db: bool = False,
    owner_disabled: bool = False,
    shard_count: int = 3,
) -> dict[str, dict[str, Any]]:
    """Return a small in-memory desired-state inventory used by unit tests."""

    key = name.lower()
    databases: dict[str, dict[str, Any]] = {}

    if with_db:
        databases["houseinfo"] = {
            "display_name": "HouseInfo",
            "created_at": "2026-08-01T12:00:00Z",
            "owner_disabled": owner_disabled,
            "owner_disabled_at": (
                "2026-08-31T12:00:00Z" if owner_disabled else ""
            ),
            "rotation_version": 1,
            "rotated_at": "2026-08-01T12:00:00Z",
        }

    is_sharded = deployment_type == "ShardedCluster"
    return {
        key: {
            "display_name": name,
            "deployment_type": deployment_type,
            "created_at": "2026-08-01T11:00:00Z",
            "members": 3,
            "version": "8.0.29",
            "persistent": True,
            "storage_class": "mongodb-data-local",
            "storage_size": "16Gi",
            "storage_mode": "static-local",
            "storage_base_path": "/tmp/mongodb",
            "storage_node_name": "node-0",
            "controller_password_version": 1,
            "shard_count": shard_count if is_sharded else 0,
            "storage_shard_count": shard_count if is_sharded else 0,
            "members_per_shard": 3 if is_sharded else 0,
            "mongos_count": 2 if is_sharded else 0,
            "config_server_count": 3 if is_sharded else 0,
            "databases": databases,
        }
    }


def online_sc_status(
    shards: int = 3, cluster: str = "sc9"
) -> dict[str, Any]:
    """Return a healthy ShardedCluster status structure."""

    return {
        "phase": "Running",
        "message": "",
        "shards": [
            {
                "shard": f"{cluster}-{index}",
                "name": f"{cluster}-{index}",
                "status": "Online",
                "desired": 3,
                "ready": 3,
                "updated": 3,
            }
            for index in range(shards)
        ],
        "config_servers": {
            "name": f"{cluster}-config",
            "status": "Online",
            "desired": 3,
            "ready": 3,
            "updated": 3,
        },
        "mongos": {
            "name": f"{cluster}-mongos",
            "status": "Online",
            "desired": 2,
            "ready": 2,
            "updated": 2,
        },
    }


def topology_lock(
    *,
    action: str = "AddShard",
    start: int = 3,
    target: int = 5,
) -> dict[str, Any]:
    """Return a representative active topology deployment lock."""

    return {
        "operation_id": "op-123",
        "category": "topology",
        "action": action,
        "database": "",
        "start_shards": start,
        "target_shards": target,
        "started_at": "2026-09-08T12:00:00Z",
    }


class FakeVault:
    """Minimal Vault stand-in used by controller unit tests."""

    def __init__(self, inventory: dict[str, dict[str, Any]]):
        self.inventory = inventory

    def load_inventory(self) -> dict[str, dict[str, Any]]:
        """Return the current in-memory desired-state inventory."""

        return self.inventory
