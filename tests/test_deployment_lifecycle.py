"""Unit tests for ReplicaSet, ShardedCluster, and shard lifecycle mutations."""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from privateWorkerReplacement import deployments

from helpers import FakeVault, deployment_inventory, topology_lock


class DeploymentLifecycleTests(unittest.TestCase):
    """Verify topology mutations, safety checks, and cleanup ordering."""

    def setUp(self) -> None:
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

    def test_add_replica_set_verifies_controller_auth_before_success(self) -> None:
        vault = FakeVault({})
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append(copy.deepcopy(operation))

        with (
            patch.object(deployments, "apply_inventory", side_effect=apply_side_effect),
            patch.object(deployments.kube, "get_json", return_value=None),
            patch.object(deployments.kube, "wait_phase"),
        ):
            deployments.add_replica_set(self.config, vault, "RS1")

        self.assertIsNone(calls[0])
        self.assertEqual(calls[1]["action"], "verify_controller_admin")
        self.assertEqual(calls[1]["deployment"], "rs1")
        self.assertEqual(calls[1]["deployment_type"], "ReplicaSet")

    def test_add_sharded_cluster_verifies_controller_auth_before_success(self) -> None:
        vault = FakeVault({})
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append(copy.deepcopy(operation))

        with (
            patch.object(deployments, "apply_inventory", side_effect=apply_side_effect),
            patch.object(deployments.kube, "get_json", return_value=None),
            patch.object(deployments.kube, "wait_sharded_cluster_ready"),
            patch.object(deployments.kube, "wait_phase"),
        ):
            deployments.add_sharded_cluster(self.config, vault, "SC9", 2)

        self.assertIsNone(calls[0])
        self.assertEqual(calls[1]["action"], "verify_controller_admin")
        self.assertEqual(calls[1]["deployment"], "sc9")
        self.assertEqual(calls[1]["deployment_type"], "ShardedCluster")

    def test_add_sharded_cluster_records_topology(self) -> None:
        vault = FakeVault({})
        calls = []

        with (
            patch.object(
                deployments,
                "apply_inventory",
                side_effect=lambda c, i, operation=None: calls.append(
                    copy.deepcopy(i)
                ),
            ),
            patch.object(deployments.kube, "get_json", return_value=None),
            patch.object(deployments.kube, "wait_sharded_cluster_ready"),
            patch.object(deployments.kube, "wait_phase"),
        ):
            deployments.add_sharded_cluster(self.config, vault, "SC9", 4)

        sc = calls[0]["sc9"]
        self.assertEqual(sc["deployment_type"], "ShardedCluster")
        self.assertEqual(sc["shard_count"], 4)
        self.assertEqual(sc["storage_shard_count"], 4)

    def test_delete_replica_set_removes_ops_manager_project(self) -> None:
        vault = FakeVault(
            deployment_inventory(
                deployment_type="ReplicaSet",
                name="RS1",
            )
        )

        with (
            patch.object(deployments, "require_no_active_change"),
            patch.object(deployments, "require_running"),
            patch.object(deployments, "apply_inventory") as apply_mock,
            patch.object(deployments.kube, "wait_absent") as wait_mock,
            patch.object(
                deployments,
                "delete_ops_manager_project",
            ) as project_delete_mock,
        ):
            deployments.delete_replica_set(
                self.config,
                vault,
                "RS1",
                True,
            )

        self.assertEqual(apply_mock.call_count, 2)
        wait_mock.assert_called_once_with(
            self.config,
            "mongodb",
            "rs1",
            self.config["rs_ready_timeout"],
        )
        project_delete_mock.assert_called_once_with(
            self.config,
            "RS1",
            timeout=self.config["rs_ready_timeout"],
        )
        self.assertNotIn("rs1", vault.inventory)

    def test_delete_sharded_cluster_removes_ops_manager_project(self) -> None:
        vault = FakeVault(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC9",
            )
        )

        with (
            patch.object(deployments, "require_no_active_change"),
            patch.object(deployments, "require_running"),
            patch.object(deployments, "apply_inventory") as apply_mock,
            patch.object(deployments.kube, "wait_absent") as wait_mock,
            patch.object(
                deployments,
                "delete_ops_manager_project",
            ) as project_delete_mock,
        ):
            deployments.delete_sharded_cluster(
                self.config,
                vault,
                "SC9",
                True,
            )

        self.assertEqual(apply_mock.call_count, 2)
        wait_mock.assert_called_once_with(
            self.config,
            "mongodb",
            "sc9",
            self.config["sc_ready_timeout"],
        )
        project_delete_mock.assert_called_once_with(
            self.config,
            "SC9",
            timeout=self.config["sc_ready_timeout"],
        )
        self.assertNotIn("sc9", vault.inventory)

    def test_add_two_shards_prepares_storage_before_live_count(self) -> None:
        vault = FakeVault(
            deployment_inventory(deployment_type="ShardedCluster", name="SC9")
        )
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append(copy.deepcopy(inventory["sc9"]))

        with (
            patch.object(deployments, "read_deployment_lock", return_value=None),
            patch.object(deployments, "require_running"),
            patch.object(
                deployments,
                "acquire_deployment_lock",
                return_value=topology_lock(start=3, target=5),
            ),
            patch.object(deployments, "release_deployment_lock"),
            patch.object(
                deployments,
                "apply_inventory",
                side_effect=apply_side_effect,
            ),
            patch.object(deployments.kube, "wait_sharded_cluster_ready"),
        ):
            deployments.add_shard(self.config, vault, "SC9", 2)

        self.assertEqual(calls[0]["storage_shard_count"], 5)
        self.assertEqual(calls[0]["shard_count"], 3)
        self.assertEqual(calls[1]["shard_count"], 5)

    def test_delete_shard_allowed_when_managed_database_exists(self) -> None:
        vault = FakeVault(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC9",
                with_db=True,
            )
        )
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append((copy.deepcopy(inventory["sc9"]), copy.deepcopy(operation)))

        with (
            patch.object(deployments, "read_deployment_lock", return_value=None),
            patch.object(deployments, "require_running"),
            patch.object(
                deployments,
                "acquire_deployment_lock",
                return_value=topology_lock(action="DeleteShard", start=3, target=2),
            ),
            patch.object(deployments, "release_deployment_lock"),
            patch.object(
                deployments,
                "apply_inventory",
                side_effect=apply_side_effect,
            ),
            patch.object(deployments.kube, "wait_sharded_cluster_ready"),
            patch.object(deployments.kube, "wait_absent"),
        ):
            deployments.delete_shard(self.config, vault, "SC9", 1, True)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0]["shard_count"], 2)
        self.assertEqual(calls[0][0]["storage_shard_count"], 3)
        self.assertEqual(calls[1][0]["shard_count"], 2)
        self.assertEqual(calls[1][0]["storage_shard_count"], 2)
        self.assertIsNone(calls[0][1])
        self.assertIn("houseinfo", vault.inventory["sc9"]["databases"])

    def test_delete_shards_cannot_reduce_cluster_below_one(self) -> None:
        vault = FakeVault(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC9",
                shard_count=2,
            )
        )

        with patch.object(deployments, "read_deployment_lock", return_value=None):
            with self.assertRaises(deployments.ControllerError) as ctx:
                deployments.delete_shard(self.config, vault, "SC9", 2, True)

        self.assertIn("must retain at least 1 shard", str(ctx.exception))
        self.assertIn("Maximum deletable now: 1", str(ctx.exception))

    def test_interrupted_add_shard_resumes_original_target(self) -> None:
        inventory = deployment_inventory(
            deployment_type="ShardedCluster",
            name="SC9",
            shard_count=4,
        )
        # An earlier AddShard 3 -> 5 already advanced desired state to 4.
        inventory["sc9"]["storage_shard_count"] = 5
        vault = FakeVault(inventory)
        lock = topology_lock(start=3, target=5)

        with (
            patch.object(deployments, "read_deployment_lock", return_value=lock),
            patch.object(deployments, "apply_inventory") as apply_mock,
            patch.object(deployments.kube, "wait_sharded_cluster_ready"),
            patch.object(deployments, "release_deployment_lock"),
        ):
            deployments.add_shard(self.config, vault, "SC9", 2)

        # The resume target is five, not current four plus two (= six).
        self.assertEqual(inventory["sc9"]["shard_count"], 5)
        self.assertEqual(apply_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
