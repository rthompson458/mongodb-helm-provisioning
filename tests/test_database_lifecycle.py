"""Unit tests for database and credential lifecycle orchestration."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from terraform_controller import databases, deployment_lock, deployments

from helpers import FakeVault, deployment_inventory, online_sc_status, topology_lock


class DatabaseLifecycleTests(unittest.TestCase):
    """Verify database operations are ordered, guarded, and Terraform-driven."""

    def setUp(self) -> None:
        self.config = {
            "rotation_days": 30,
            "rs_ready_timeout": 30,
            "sc_ready_timeout": 30,
            "vault_address": "http://127.0.0.1:8200",
            "vault_base_path": "mongodb",
        }

    def test_add_database_materializes_before_accounts_are_added(self) -> None:
        vault = FakeVault(deployment_inventory())
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append((copy.deepcopy(inventory), copy.deepcopy(operation)))

        with (
            patch.object(databases, "require_running"),
            patch.object(databases, "apply_inventory", side_effect=apply_side_effect),
            patch.object(databases, "_verify_database_accounts"),
            patch.object(
                databases,
                "utc_now",
                return_value=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc),
            ),
        ):
            databases.add_database(self.config, vault, "RS1", "HouseInfo")

        self.assertEqual(calls[0][1]["action"], "create_database")
        self.assertEqual(calls[0][1]["deployment"], "rs1")
        self.assertEqual(calls[0][0]["rs1"]["databases"], {})
        self.assertIsNone(calls[1][1])
        self.assertIn("houseinfo", calls[1][0]["rs1"]["databases"])

    def test_add_database_uses_only_deployment_when_target_omitted(self) -> None:
        vault = FakeVault(deployment_inventory())
        calls = []

        with (
            patch.object(databases, "require_running"),
            patch.object(
                databases,
                "apply_inventory",
                side_effect=lambda c, i, operation=None: calls.append(
                    (copy.deepcopy(i), copy.deepcopy(operation))
                ),
            ),
            patch.object(databases, "_verify_database_accounts"),
            patch.object(
                databases,
                "utc_now",
                return_value=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc),
            ),
        ):
            databases.add_database(self.config, vault, "HouseInfo")

        self.assertEqual(calls[0][1]["deployment"], "rs1")
        self.assertEqual(calls[0][1]["database"], "HouseInfo")

    def test_implicit_target_rejected_when_multiple_deployments_exist(self) -> None:
        inventory = deployment_inventory()
        inventory.update(
            deployment_inventory(deployment_type="ShardedCluster", name="SC9")
        )
        vault = FakeVault(inventory)

        with self.assertRaises(databases.ControllerError) as ctx:
            databases.add_database(self.config, vault, "HouseInfo")

        self.assertIn("Multiple MongoDB deployments exist", str(ctx.exception))

    def test_sharded_database_change_requires_every_shard_online(self) -> None:
        vault = FakeVault(
            deployment_inventory(deployment_type="ShardedCluster", name="SC9")
        )
        status = online_sc_status()
        status["shards"][1]["status"] = "Creating"
        status["shards"][1]["ready"] = 1

        with (
            patch.object(deployment_lock, "read_deployment_lock", return_value=None),
            patch.object(deployments.kube, "phase", return_value="Running"),
            patch.object(
                deployments.kube,
                "sharded_cluster_status",
                return_value=status,
            ),
            patch.object(databases, "apply_inventory") as apply_mock,
        ):
            with self.assertRaises(databases.ControllerError) as ctx:
                databases.add_database(self.config, vault, "SC9", "HouseInfo")

        self.assertIn("not ready for database work", str(ctx.exception))
        self.assertIn("Creating", str(ctx.exception))
        apply_mock.assert_not_called()

    def test_active_shard_change_blocks_database_creation(self) -> None:
        vault = FakeVault(
            deployment_inventory(deployment_type="ShardedCluster", name="SC9")
        )

        with (
            patch.object(
                deployment_lock,
                "read_deployment_lock",
                return_value=topology_lock(),
            ),
            patch.object(databases, "apply_inventory") as apply_mock,
        ):
            with self.assertRaises(databases.ControllerError) as ctx:
                databases.add_database(self.config, vault, "SC9", "HouseInfo")

        self.assertIn("busy with another managed change", str(ctx.exception))
        self.assertIn("AddShard 3 -> 5", str(ctx.exception))
        apply_mock.assert_not_called()

    def test_rotate_passwords_is_requested_as_terraform_operation(self) -> None:
        vault = FakeVault(deployment_inventory(with_db=True))
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append((copy.deepcopy(inventory), copy.deepcopy(operation)))
            if operation and operation.get("action") == "rotate_passwords":
                db = vault.inventory["rs1"]["databases"]["houseinfo"]
                db["rotation_version"] += 1
                db["rotated_at"] = "2026-09-06T12:00:00Z"
                db["owner_disabled"] = True
                db["owner_disabled_at"] = "2026-09-06T12:00:00Z"

        with (
            patch.object(databases, "require_running"),
            patch.object(databases, "apply_inventory", side_effect=apply_side_effect),
            patch.object(databases, "_verify_database_accounts"),
        ):
            databases.rotate_passwords(self.config, vault, "RS1", "HouseInfo")

        self.assertEqual(calls[0][1]["action"], "rotate_passwords")
        self.assertEqual(calls[0][1]["deployment"], "rs1")

    def test_rotation_recovers_after_partial_apply(self) -> None:
        vault = FakeVault(deployment_inventory(with_db=True))
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append((copy.deepcopy(inventory), copy.deepcopy(operation)))
            db = vault.inventory["rs1"]["databases"]["houseinfo"]
            if len(calls) == 1:
                db["rotation_version"] = 2
                db["rotated_at"] = "2026-09-06T12:00:00Z"
                raise databases.ControllerError("simulated partial apply")
            db["rotation_version"] = 3
            db["rotated_at"] = "2026-09-06T12:01:00Z"

        with (
            patch.object(databases, "require_running"),
            patch.object(databases, "apply_inventory", side_effect=apply_side_effect),
            patch.object(databases, "_verify_database_accounts"),
        ):
            databases.rotate_passwords(self.config, vault, "RS1", "HouseInfo")

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]["action"], "rotate_passwords")

    def test_delete_database_drops_before_removing_desired_state(self) -> None:
        vault = FakeVault(deployment_inventory(with_db=True))
        calls = []

        with (
            patch.object(databases, "require_running"),
            patch.object(databases.kube, "wait_absent"),
            patch.object(
                databases,
                "apply_inventory",
                side_effect=lambda c, i, operation=None: calls.append(
                    (copy.deepcopy(i), copy.deepcopy(operation))
                ),
            ),
        ):
            databases.delete_database(
                self.config, vault, "RS1", "HouseInfo", True
            )

        self.assertEqual(calls[0][1]["action"], "delete_database")
        self.assertIn("houseinfo", calls[0][0]["rs1"]["databases"])
        self.assertNotIn("houseinfo", calls[1][0]["rs1"]["databases"])
        self.assertEqual(calls[2][1]["action"], "verify_database_users_absent")


if __name__ == "__main__":
    unittest.main()
