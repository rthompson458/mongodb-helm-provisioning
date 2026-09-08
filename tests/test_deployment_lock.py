"""Unit tests for the ShardedCluster deployment-lock component."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from terraform_controller import deployment_lock

from helpers import FakeVault, deployment_inventory, topology_lock


class DeploymentLockTests(unittest.TestCase):
    """Verify lock parsing, blocking, acquire/release, and ReplicaSet bypass."""

    def setUp(self) -> None:
        self.config = {}

    def test_require_no_active_change_blocks_sharded_cluster(self) -> None:
        deployment = deployment_inventory(
            deployment_type="ShardedCluster", name="SC9"
        )["sc9"]

        with patch.object(
            deployment_lock,
            "read_deployment_lock",
            return_value=topology_lock(),
        ):
            with self.assertRaises(deployment_lock.ControllerError) as ctx:
                deployment_lock.require_no_active_change(
                    self.config, "sc9", deployment
                )

        self.assertIn("busy with another managed change", str(ctx.exception))

    def test_replica_set_does_not_use_sharded_cluster_lock(self) -> None:
        deployment = deployment_inventory()["rs1"]

        with patch.object(
            deployment_lock, "read_deployment_lock"
        ) as read_mock:
            deployment_lock.require_no_active_change(
                self.config, "rs1", deployment
            )

        read_mock.assert_not_called()

    def test_acquire_lock_is_requested_through_terraform(self) -> None:
        inventory = deployment_inventory(
            deployment_type="ShardedCluster", name="SC9"
        )
        deployment = inventory["sc9"]
        observed = topology_lock(start=3, target=5)

        with (
            patch.object(
                deployment_lock,
                "apply_inventory",
            ) as apply_mock,
            patch.object(
                deployment_lock,
                "read_deployment_lock",
                return_value=observed,
            ),
            patch.object(
                deployment_lock.uuid,
                "uuid4",
            ) as uuid_mock,
        ):
            uuid_mock.return_value.hex = "op-123"
            result = deployment_lock.acquire_deployment_lock(
                self.config,
                inventory,
                "sc9",
                deployment,
                category="topology",
                action="AddShard",
                start_shards=3,
                target_shards=5,
            )

        operation = apply_mock.call_args.args[2]
        self.assertEqual(operation["action"], "acquire_deployment_lock")
        self.assertEqual(operation["lock_category"], "topology")
        self.assertEqual(operation["lock_action"], "AddShard")
        self.assertEqual(result["operation_id"], "op-123")

    def test_release_lock_verifies_absence_after_terraform(self) -> None:
        inventory = deployment_inventory(
            deployment_type="ShardedCluster", name="SC9"
        )
        deployment = inventory["sc9"]
        lock = topology_lock()

        with (
            patch.object(
                deployment_lock,
                "apply_inventory",
            ) as apply_mock,
            patch.object(
                deployment_lock,
                "read_deployment_lock",
                return_value=None,
            ),
        ):
            deployment_lock.release_deployment_lock(
                self.config, inventory, "sc9", deployment, lock
            )

        operation = apply_mock.call_args.args[2]
        self.assertEqual(operation["action"], "release_deployment_lock")
        self.assertEqual(operation["operation_id"], "op-123")

    def test_protected_database_change_releases_using_latest_inventory(self) -> None:
        inventory = deployment_inventory(
            deployment_type="ShardedCluster", name="SC9"
        )
        vault = FakeVault(inventory)
        lock = {
            **topology_lock(start=3, target=3),
            "category": "database",
            "action": "AddDatabase",
            "database": "HouseInfo",
        }

        with (
            patch.object(
                deployment_lock,
                "acquire_deployment_lock",
                return_value=lock,
            ),
            patch.object(
                deployment_lock,
                "release_deployment_lock",
            ) as release_mock,
        ):
            with deployment_lock.protected_database_change(
                self.config,
                vault,
                inventory,
                "sc9",
                inventory["sc9"],
                "AddDatabase",
                "HouseInfo",
            ):
                inventory["sc9"]["databases"]["houseinfo"] = {
                    "display_name": "HouseInfo"
                }

        released_inventory = release_mock.call_args.args[1]
        self.assertIn(
            "houseinfo", released_inventory["sc9"]["databases"]
        )


if __name__ == "__main__":
    unittest.main()
