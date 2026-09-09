"""Unit tests for Reconcile safety and convergence orchestration."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from terraform_controller import maintenance

from helpers import FakeVault, deployment_inventory, online_sc_status, topology_lock


class MaintenanceTests(unittest.TestCase):
    """Verify Reconcile does not race with active ShardedCluster mutations."""

    def setUp(self) -> None:
        self.config = {
            "rs_ready_timeout": 30,
            "sc_ready_timeout": 30,
        }

    def test_list_managed_resources_reports_clean_zero_state(self) -> None:
        vault = FakeVault({})

        def fake_list(_config, resource, **kwargs):
            self.assertIn(resource, {"mongodb", "pvc", "pv", "configmap"})
            if resource == "pv":
                self.assertFalse(kwargs.get("namespaced", True))
            return []

        output = io.StringIO()
        with (
            patch.object(maintenance.kube, "list_json", side_effect=fake_list),
            redirect_stdout(output),
        ):
            maintenance.list_managed_resources(self.config, vault)

        text = output.getvalue()
        self.assertRegex(text, r"(?m)^Managed deployments:\s+0$")
        self.assertRegex(text, r"(?m)^MongoDB resources:\s+0$")
        self.assertRegex(text, r"(?m)^PVCs:\s+0$")
        self.assertRegex(text, r"(?m)^PVs:\s+0$")
        self.assertRegex(text, r"(?m)^Deployment locks:\s+0$")
        self.assertIn("Status: CLEAN", text)

    def test_list_managed_resources_reports_remaining_resource_names(self) -> None:
        vault = FakeVault(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC9",
            )
        )

        def fake_list(_config, resource, **kwargs):
            if resource == "mongodb":
                return [{"metadata": {"name": "sc9"}}]
            if resource == "pvc":
                return [{"metadata": {"name": "data-sc9-0-0"}}]
            if resource == "pv":
                return [{"metadata": {"name": "sc9-shard-0"}}]
            if resource == "configmap":
                return [
                    {"metadata": {"name": "unrelated-config"}},
                    {"metadata": {"name": "tc-deployment-lock-sc9"}},
                ]
            raise AssertionError(resource)

        output = io.StringIO()
        with (
            patch.object(maintenance.kube, "list_json", side_effect=fake_list),
            redirect_stdout(output),
        ):
            maintenance.list_managed_resources(self.config, vault)

        text = output.getvalue()
        self.assertRegex(text, r"(?m)^Managed deployments:\s+1$")
        self.assertRegex(text, r"(?m)^MongoDB resources:\s+1$")
        self.assertRegex(text, r"(?m)^PVCs:\s+1$")
        self.assertRegex(text, r"(?m)^PVs:\s+1$")
        self.assertRegex(text, r"(?m)^Deployment locks:\s+1$")
        self.assertIn("Status: ATTENTION REQUIRED", text)
        self.assertIn("SC9", text)
        self.assertIn("sc9", text)
        self.assertIn("data-sc9-0-0", text)
        self.assertIn("sc9-shard-0", text)
        self.assertIn("tc-deployment-lock-sc9", text)
        self.assertNotIn("unrelated-config", text)

    def test_reconcile_with_no_inventory_is_noop(self) -> None:
        vault = FakeVault({})
        with patch.object(maintenance, "apply_inventory") as apply_mock:
            maintenance.reconcile(self.config, vault)
        apply_mock.assert_not_called()

    def test_reconcile_blocked_by_active_sharded_cluster_lock(self) -> None:
        inventory = deployment_inventory(
            deployment_type="ShardedCluster", name="SC9"
        )
        vault = FakeVault(inventory)

        with (
            patch.object(
                maintenance,
                "read_deployment_lock",
                return_value=topology_lock(),
            ),
            patch.object(maintenance, "apply_inventory") as apply_mock,
        ):
            with self.assertRaises(maintenance.ControllerError) as ctx:
                maintenance.reconcile(self.config, vault)

        self.assertIn("Reconcile is blocked", str(ctx.exception))
        apply_mock.assert_not_called()


    def test_recover_completed_delete_shard_releases_only_lock(self) -> None:
        inventory = deployment_inventory(
            deployment_type="ShardedCluster",
            name="SC9",
            shard_count=3,
        )
        vault = FakeVault(inventory)
        lock = topology_lock(action="DeleteShard", start=4, target=3)

        def fake_get_json(_config, resource, name):
            if resource == "mongodb" and name == "sc9":
                return {"spec": {"shardCount": 3}, "status": {"phase": "Running"}}
            if resource == "statefulset" and name == "sc9-3":
                return None
            return None

        with (
            patch.object(maintenance, "read_deployment_lock", return_value=lock),
            patch.object(maintenance.kube, "get_json", side_effect=fake_get_json),
            patch.object(
                maintenance.kube,
                "sharded_cluster_status",
                return_value=online_sc_status(3, "sc9"),
            ),
            patch.object(maintenance, "release_deployment_lock") as release_mock,
        ):
            maintenance.recover_deployment_lock(
                self.config, vault, "SC9", confirmed=True
            )

        release_mock.assert_called_once()
        self.assertEqual(
            release_mock.call_args.kwargs["targets"],
            ["terraform_data.lifecycle_operation"],
        )

    def test_recover_lock_refuses_before_target_topology_is_healthy(self) -> None:
        inventory = deployment_inventory(
            deployment_type="ShardedCluster",
            name="SC9",
            shard_count=3,
        )
        vault = FakeVault(inventory)
        lock = topology_lock(action="DeleteShard", start=4, target=3)
        degraded = online_sc_status(3, "sc9")
        degraded["shards"][2]["status"] = "Degraded"

        with (
            patch.object(maintenance, "read_deployment_lock", return_value=lock),
            patch.object(
                maintenance.kube,
                "get_json",
                return_value={"spec": {"shardCount": 3}},
            ),
            patch.object(
                maintenance.kube,
                "sharded_cluster_status",
                return_value=degraded,
            ),
            patch.object(maintenance, "release_deployment_lock") as release_mock,
        ):
            with self.assertRaises(maintenance.ControllerError):
                maintenance.recover_deployment_lock(
                    self.config, vault, "SC9", confirmed=True
                )

        release_mock.assert_not_called()


    def test_recover_orphaned_resources_applies_empty_inventory(self) -> None:
        vault = FakeVault({})

        with (
            patch.object(maintenance.kube, "list_json", return_value=[]),
            patch.object(maintenance, "apply_inventory") as apply_mock,
        ):
            maintenance.recover_orphaned_resources(
                self.config, vault, confirmed=True
            )

        apply_mock.assert_called_once_with(self.config, {})

    def test_recover_orphaned_resources_refuses_nonempty_vault_inventory(self) -> None:
        vault = FakeVault(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC9",
            )
        )

        with (
            patch.object(maintenance.kube, "list_json") as list_mock,
            patch.object(maintenance, "apply_inventory") as apply_mock,
        ):
            with self.assertRaises(maintenance.ControllerError) as ctx:
                maintenance.recover_orphaned_resources(
                    self.config, vault, confirmed=True
                )

        self.assertIn("Vault-backed controller inventory is empty", str(ctx.exception))
        list_mock.assert_not_called()
        apply_mock.assert_not_called()

    def test_recover_orphaned_resources_refuses_live_managed_mongodb(self) -> None:
        vault = FakeVault({})
        live = [{"metadata": {"name": "sc9"}}]

        with (
            patch.object(maintenance.kube, "list_json", return_value=live),
            patch.object(maintenance, "apply_inventory") as apply_mock,
        ):
            with self.assertRaises(maintenance.ControllerError) as ctx:
                maintenance.recover_orphaned_resources(
                    self.config, vault, confirmed=True
                )

        self.assertIn("live terraformController-managed MongoDB", str(ctx.exception))
        apply_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
