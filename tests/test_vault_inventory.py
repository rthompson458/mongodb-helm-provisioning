"""Unit tests for Vault metadata-to-inventory reconstruction."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from privateWorkerReplacement.common import ControllerError
from privateWorkerReplacement.vault import VaultClient


CONFIG = {
    "vault_address": "http://127.0.0.1:8200",
    "vault_mount": "secret",
    "vault_base_path": "mongodb",
    "vault_token_env": "TEST_VAULT_TOKEN",
    "storage_mode": "static-local",
    "storage_base_path": "/tmp/mongodb",
    "storage_node_name": "node-0",
    "default_shards": 3,
    "default_members_per_shard": 3,
    "default_mongos": 2,
    "default_config_servers": 3,
}


class VaultInventoryTests(unittest.TestCase):
    """Verify current and legacy Vault metadata remain reconstructable."""

    def _client(self) -> VaultClient:
        with patch.dict(os.environ, {"TEST_VAULT_TOKEN": "test-token"}):
            return VaultClient(CONFIG)

    def test_missing_token_is_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ControllerError):
                VaultClient(CONFIG)

    def test_sharded_metadata_builds_complete_deployment(self) -> None:
        client = self._client()
        deployment = client._new_deployment(
            {
                "display_name": "SC9",
                "deployment_type": "ShardedCluster",
                "created_at": "2026-09-08T12:00:00Z",
                "members": "3",
                "version": "8.0.29",
                "persistent": "true",
                "storage_class": "mongodb-data-local",
                "storage_size": "16Gi",
                "shard_count": "4",
                "storage_shard_count": "4",
                "members_per_shard": "3",
                "mongos_count": "2",
                "config_server_count": "3",
            }
        )

        self.assertEqual(deployment["deployment_type"], "ShardedCluster")
        self.assertEqual(deployment["shard_count"], 4)
        self.assertEqual(deployment["members_per_shard"], 3)
        self.assertEqual(deployment["databases"], {})

    def test_unknown_deployment_type_is_rejected(self) -> None:
        client = self._client()
        with self.assertRaises(ControllerError):
            client._new_deployment(
                {
                    "display_name": "BAD",
                    "deployment_type": "Mystery",
                }
            )

    def test_current_layout_wins_over_legacy_layout(self) -> None:
        client = self._client()
        current = {"rs1": {"display_name": "CURRENT", "databases": {}}}
        legacy = {"rs1": {"display_name": "LEGACY", "databases": {}}}

        with (
            patch.object(client, "_load_current_layout", return_value=current),
            patch.object(client, "_load_legacy_layout", return_value=legacy),
        ):
            inventory = client.load_inventory()

        self.assertEqual(inventory["rs1"]["display_name"], "CURRENT")


if __name__ == "__main__":
    unittest.main()
