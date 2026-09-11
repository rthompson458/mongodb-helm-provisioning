"""Unit tests for ShardedCluster shard-topology lifecycle behavior."""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from privateWorkerReplacement import shards

from helpers import FakeVault, deployment_inventory, topology_lock


class ShardLifecycleTests(unittest.TestCase):
    """Verify shard scale-up/down ordering, safety, and resume behavior."""

    def setUp(self) -> None:
        """Provide the runtime defaults used by shard lifecycle unit tests."""

        self.config = {
            "default_members": 3,
            "default_version": "8.0.29",
            "persistent": True,
            "storage_class": "mongodb-data-local",
            "storage_size": "16Gi",
            "storage_mode": "static-local",
            "storage_base_path": "/tmp/mongodb",
            "storage_node_name": "node-0",
            "default_shards": 3,
            "default_members_per_shard": 3,
            "default_mongos": 2,
            "default_config_servers": 3,
            "rs_ready_timeout": 30,
            "sc_ready_timeout": 30,
        }

    def test_add_two_shards_prepares_storage_before_live_count(self) -> None:
        """Scale-up must prepare storage before increasing live shardCount."""

        vault = FakeVault(
            deployment_inventory(deployment_type="ShardedCluster", name="SC9")
        )
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            """Capture the desired deployment state sent to Terraform."""

            calls.append(copy.deepcopy(inventory["sc9"]))

        with (
            patch.object(shards, "read_deployment_lock", return_value=None),
            patch.object(shards, "require_running"),
            patch.object(
                shards,
                "acquire_deployment_lock",
                return_value=topology_lock(start=3, target=5),
            ),
            patch.object(shards, "release_deployment_lock"),
            patch.object(
                shards,
                "apply_inventory",
                side_effect=apply_side_effect,
            ),
            patch.object(shards.kube, "wait_sharded_cluster_ready"),
        ):
            shards.add_shard(self.config, vault, "SC9", 2)

        self.assertEqual(calls[0]["storage_shard_count"], 5)
        self.assertEqual(calls[0]["shard_count"], 3)
        self.assertEqual(calls[1]["shard_count"], 5)

    def test_delete_shard_allowed_when_managed_database_exists(self) -> None:
        """Shard contraction must preserve managed databases on the cluster."""

        vault = FakeVault(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC9",
                with_db=True,
            )
        )
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            """Capture desired state and one-shot operation for each apply."""

            calls.append((copy.deepcopy(inventory["sc9"]), copy.deepcopy(operation)))

        with (
            patch.object(shards, "read_deployment_lock", return_value=None),
            patch.object(shards, "require_running"),
            patch.object(
                shards,
                "acquire_deployment_lock",
                return_value=topology_lock(action="DeleteShard", start=3, target=2),
            ),
            patch.object(shards, "release_deployment_lock"),
            patch.object(
                shards,
                "apply_inventory",
                side_effect=apply_side_effect,
            ),
            patch.object(shards.kube, "wait_sharded_cluster_ready"),
            patch.object(shards.kube, "wait_absent"),
        ):
            shards.delete_shard(self.config, vault, "SC9", 1, True)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0]["shard_count"], 2)
        self.assertEqual(calls[0][0]["storage_shard_count"], 3)
        self.assertEqual(calls[1][0]["shard_count"], 2)
        self.assertEqual(calls[1][0]["storage_shard_count"], 2)
        self.assertIsNone(calls[0][1])
        self.assertIn("houseinfo", vault.inventory["sc9"]["databases"])

    def test_delete_shards_cannot_reduce_cluster_below_one(self) -> None:
        """DeleteShard must refuse any request whose target would be zero."""

        vault = FakeVault(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC9",
                shard_count=2,
            )
        )

        with patch.object(shards, "read_deployment_lock", return_value=None):
            with self.assertRaises(shards.ControllerError) as ctx:
                shards.delete_shard(self.config, vault, "SC9", 2, True)

        self.assertIn("must retain at least 1 shard", str(ctx.exception))
        self.assertIn("Maximum deletable now: 1", str(ctx.exception))

    def test_interrupted_add_shard_resumes_original_target(self) -> None:
        """A retry must resume the lock target instead of adding COUNT again."""

        inventory = deployment_inventory(
            deployment_type="ShardedCluster",
            name="SC9",
            shard_count=4,
        )
        inventory["sc9"]["storage_shard_count"] = 5
        vault = FakeVault(inventory)
        lock = topology_lock(start=3, target=5)

        with (
            patch.object(shards, "read_deployment_lock", return_value=lock),
            patch.object(shards, "apply_inventory") as apply_mock,
            patch.object(shards.kube, "wait_sharded_cluster_ready"),
            patch.object(shards, "release_deployment_lock"),
        ):
            shards.add_shard(self.config, vault, "SC9", 2)

        self.assertEqual(inventory["sc9"]["shard_count"], 5)
        self.assertEqual(apply_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
