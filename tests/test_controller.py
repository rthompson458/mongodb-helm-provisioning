from __future__ import annotations

import copy
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch

from terraform_controller import cli, databases, deployment_lock, deployments


def deployment_inventory(
    *,
    deployment_type: str = "ReplicaSet",
    name: str = "RS1",
    with_db: bool = False,
    owner_disabled: bool = False,
    shard_count: int = 3,
):
    key = name.lower()
    dbs = {}
    if with_db:
        dbs["houseinfo"] = {
            "display_name": "HouseInfo",
            "created_at": "2026-08-01T12:00:00Z",
            "owner_disabled": owner_disabled,
            "owner_disabled_at": "2026-08-31T12:00:00Z" if owner_disabled else "",
            "rotation_version": 1,
            "rotated_at": "2026-08-01T12:00:00Z",
        }
    is_sc = deployment_type == "ShardedCluster"
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
            "shard_count": shard_count if is_sc else 0,
            "storage_shard_count": shard_count if is_sc else 0,
            "members_per_shard": 3 if is_sc else 0,
            "mongos_count": 2 if is_sc else 0,
            "config_server_count": 3 if is_sc else 0,
            "databases": dbs,
        }
    }


def online_sc_status(shards: int = 3, cluster: str = "sc9"):
    return {
        "phase": "Running",
        "message": "",
        "shards": [
            {
                "shard": f"{cluster}-{i}",
                "name": f"{cluster}-{i}",
                "status": "Online",
                "desired": 3,
                "ready": 3,
                "updated": 3,
            }
            for i in range(shards)
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
):
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
    def __init__(self, inventory):
        self.inventory = inventory

    def load_inventory(self):
        return self.inventory


class DatabaseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "rotation_days": 30,
            "rs_ready_timeout": 30,
            "sc_ready_timeout": 30,
            "vault_address": "http://127.0.0.1:8200",
            "vault_base_path": "mongodb",
        }

    def test_add_database_materializes_before_accounts_are_added(self):
        vault = FakeVault(deployment_inventory())
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append((copy.deepcopy(inventory), copy.deepcopy(operation)))

        with (
            patch.object(databases, "require_running"),
            patch.object(databases, "apply_inventory", side_effect=apply_side_effect),
            patch.object(databases, "_verify_database_accounts"),
            patch.object(databases, "utc_now"),
        ):
            databases.utc_now.return_value = datetime(
                2026, 9, 6, 12, 0, tzinfo=timezone.utc
            )
            databases.add_database(self.config, vault, "RS1", "HouseInfo")

        self.assertEqual(calls[0][1]["action"], "create_database")
        self.assertEqual(calls[0][1]["deployment"], "rs1")
        self.assertEqual(calls[0][0]["rs1"]["databases"], {})
        self.assertIsNone(calls[1][1])
        self.assertIn("houseinfo", calls[1][0]["rs1"]["databases"])

    def test_add_database_uses_only_deployment_when_target_omitted(self):
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

    def test_implicit_database_target_rejected_when_multiple_deployments_exist(self):
        inventory = deployment_inventory()
        inventory.update(
            deployment_inventory(deployment_type="ShardedCluster", name="SC9")
        )
        vault = FakeVault(inventory)
        with patch.object(databases.kube, "phase", return_value="Running"):
            with self.assertRaises(databases.ControllerError) as ctx:
                databases.add_database(self.config, vault, "HouseInfo")
        self.assertIn("Multiple MongoDB deployments exist", str(ctx.exception))

    def test_sharded_cluster_database_change_requires_every_shard_online(self):
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

    def test_active_shard_change_blocks_database_creation(self):
        vault = FakeVault(
            deployment_inventory(deployment_type="ShardedCluster", name="SC9")
        )
        lock = topology_lock()

        with (
            patch.object(deployment_lock, "read_deployment_lock", return_value=lock),
            patch.object(databases, "apply_inventory") as apply_mock,
        ):
            with self.assertRaises(databases.ControllerError) as ctx:
                databases.add_database(self.config, vault, "SC9", "HouseInfo")

        self.assertIn("busy with another managed change", str(ctx.exception))
        self.assertIn("AddShard 3 -> 5", str(ctx.exception))
        apply_mock.assert_not_called()

    def test_rotate_passwords_is_requested_as_terraform_operation(self):
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
        self.assertEqual(
            calls[0][0]["rs1"]["databases"]["houseinfo"]["rotation_version"],
            1,
        )

    def test_rotate_passwords_recovers_after_partial_apply(self):
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
        self.assertEqual(
            calls[0][0]["rs1"]["databases"]["houseinfo"]["rotation_version"],
            1,
        )
        self.assertEqual(
            calls[1][0]["rs1"]["databases"]["houseinfo"]["rotation_version"],
            2,
        )
        self.assertEqual(calls[1][1]["action"], "rotate_passwords")

    def test_delete_database_drops_then_removes_desired_state(self):
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
        self.assertIsNone(calls[1][1])
        self.assertNotIn("houseinfo", calls[1][0]["rs1"]["databases"])
        self.assertEqual(
            calls[2][1]["action"], "verify_database_users_absent"
        )


class DeploymentLifecycleTests(unittest.TestCase):
    def setUp(self):
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

    def test_add_sharded_cluster_records_topology(self):
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
        self.assertEqual(sc["members_per_shard"], 3)

    def test_add_two_shards_prepares_storage_before_live_count(self):
        vault = FakeVault(
            deployment_inventory(deployment_type="ShardedCluster", name="SC9")
        )
        calls = []
        lock = topology_lock(start=3, target=5)

        def apply_side_effect(config, inventory, operation=None):
            calls.append(copy.deepcopy(inventory["sc9"]))

        with (
            patch.object(deployments, "read_deployment_lock", return_value=None),
            patch.object(deployments, "require_running"),
            patch.object(
                deployments,
                "acquire_deployment_lock",
                return_value=lock,
            ),
            patch.object(
                deployments,
                "release_deployment_lock",
            ),
            patch.object(
                deployments,
                "apply_inventory",
                side_effect=apply_side_effect,
            ),
            patch.object(deployments.kube, "wait_sharded_cluster_ready"),
        ):
            deployments.add_shard(self.config, vault, "SC9", 2)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["storage_shard_count"], 5)
        self.assertEqual(calls[0]["shard_count"], 3)
        self.assertEqual(calls[1]["storage_shard_count"], 5)
        self.assertEqual(calls[1]["shard_count"], 5)

    def test_delete_shard_is_blocked_when_any_managed_database_exists(self):
        vault = FakeVault(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC9",
                with_db=True,
            )
        )
        with (
            patch.object(deployments, "read_deployment_lock", return_value=None),
            patch.object(deployments, "require_running"),
            patch.object(deployments, "apply_inventory") as apply_mock,
        ):
            with self.assertRaises(deployments.ControllerError) as ctx:
                deployments.delete_shard(
                    self.config, vault, "SC9", 1, True
                )

        self.assertIn("contains managed databases", str(ctx.exception))
        apply_mock.assert_not_called()

    def test_delete_shards_cannot_reduce_cluster_below_one(self):
        vault = FakeVault(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC9",
                shard_count=2,
            )
        )
        with patch.object(
            deployments, "read_deployment_lock", return_value=None
        ):
            with self.assertRaises(deployments.ControllerError) as ctx:
                deployments.delete_shard(
                    self.config, vault, "SC9", 2, True
                )

        self.assertIn("must retain at least 1 shard", str(ctx.exception))
        self.assertIn("Maximum deletable now: 1", str(ctx.exception))

    def test_global_list_shards_includes_all_sharded_clusters(self):
        inventory = deployment_inventory(
            deployment_type="ShardedCluster",
            name="SC9",
            shard_count=2,
        )
        inventory.update(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC10",
                shard_count=1,
            )
        )
        vault = FakeVault(inventory)

        def status(config, key, count):
            return online_sc_status(count, key)

        output = io.StringIO()
        with (
            patch.object(deployments, "read_deployment_lock", return_value=None),
            patch.object(
                deployments.kube,
                "sharded_cluster_status",
                side_effect=status,
            ),
            redirect_stdout(output),
        ):
            deployments.list_shards(self.config, vault)

        text = output.getvalue()
        self.assertIn("SC9", text)
        self.assertIn("SC10", text)
        self.assertIn("sc9-0", text)
        self.assertIn("sc10-0", text)


class CliTests(unittest.TestCase):
    def test_add_shard_accepts_optional_count(self):
        args = cli.build_parser().parse_args(["AddShard", "SC9", "2"])
        self.assertEqual(args.deployment, "SC9")
        self.assertEqual(args.count, 2)

    def test_delete_shard_defaults_to_one(self):
        args = cli.build_parser().parse_args(
            ["DeleteShard", "SC9", "--confirm"]
        )
        self.assertEqual(args.count, 1)
        self.assertTrue(args.confirm)

    def test_list_shards_cluster_is_optional(self):
        args = cli.build_parser().parse_args(["ListShards"])
        self.assertIsNone(args.deployment)


if __name__ == "__main__":
    unittest.main()
