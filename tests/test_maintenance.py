"""Unit tests for Reconcile safety and convergence orchestration."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from privateWorkerReplacement import maintenance, ops_manager

from helpers import FakeVault, deployment_inventory, online_sc_status, topology_lock


class MaintenanceTests(unittest.TestCase):
    """Verify Reconcile does not race with active ShardedCluster mutations."""

    def setUp(self) -> None:
        self.config = {
            "rs_ready_timeout": 30,
            "sc_ready_timeout": 30,
            "backend_secret_suffix": "test-state",
        }

    def test_ops_manager_projects_are_read_only_and_parsed(self) -> None:
        config = {
            "kubeconfig": "/tmp/fake-kubeconfig",
            "kube_context": "k3d-test",
            "mongodb_namespace": "mongodb",
            "ops_manager_config_map": "my-project",
            "ops_manager_credentials_secret": "organization-secret",
        }

        def fake_get(_config, resource, name):
            if resource == "configmap":
                self.assertEqual(name, "my-project")
                return {
                    "data": {
                        "baseUrl": "http://ops-manager-svc:8080",
                        "orgId": "org123",
                        "projectName": "mongodb-development",
                    }
                }

            if resource == "secret":
                self.assertEqual(name, "organization-secret")
                return {
                    "data": {
                        "publicKey": "cHVibGlj",
                        "privateKey": "cHJpdmF0ZQ==",
                    }
                }

            raise AssertionError((resource, name))

        def fake_list(_config, resource, **_kwargs):
            self.assertEqual(resource, "pod")
            return [
                {"metadata": {"name": "ops-manager-0"}},
                {"metadata": {"name": "unrelated-pod"}},
            ]

        result = unittest.mock.Mock(
            returncode=0,
            stdout=(
                '{"results":['
                '{"id":"2","name":"RSTest"},'
                '{"id":"1","name":"mongodb-development"}'
                ']}'
            ),
            stderr="",
        )

        with (
            patch.object(ops_manager.kube, "get_json", side_effect=fake_get),
            patch.object(ops_manager.kube, "list_json", side_effect=fake_list),
            patch.object(ops_manager, "run_process", return_value=result) as run_mock,
        ):
            permanent, projects = ops_manager.list_projects(config)

        self.assertEqual(permanent, "mongodb-development")
        self.assertEqual(
            projects,
            [
                {"id": "1", "name": "mongodb-development"},
                {"id": "2", "name": "RSTest"},
            ],
        )

        command = run_mock.call_args.args[0]
        self.assertIn("ops-manager-0", command)
        self.assertIn("curl", command)
        self.assertIn(
            'user = "public:private"',
            run_mock.call_args.kwargs["input_text"],
        )

    def test_ops_manager_delete_project_accepts_202_and_waits_for_absence(self) -> None:
        config = {
            "kubeconfig": "/tmp/fake-kubeconfig",
            "kube_context": "k3d-test",
            "mongodb_namespace": "mongodb",
            "ops_manager_config_map": "my-project",
            "ops_manager_credentials_secret": "organization-secret",
        }

        projects_before = (
            "mongodb-development",
            [
                {"id": "base-id", "name": "mongodb-development"},
                {"id": "rs1-id", "name": "RS1"},
            ],
        )
        projects_after = (
            "mongodb-development",
            [{"id": "base-id", "name": "mongodb-development"}],
        )

        def fake_get(_config, resource, name):
            if resource == "configmap":
                return {
                    "data": {
                        "baseUrl": "http://ops-manager-svc:8080",
                        "orgId": "org123",
                        "projectName": "mongodb-development",
                    }
                }

            if resource == "secret" and name == "organization-secret":
                return {
                    "data": {
                        "publicKey": "cHVibGlj",
                        "privateKey": "cHJpdmF0ZQ==",
                    }
                }

            if resource == "secret" and name == "rs1-id-group-secret":
                return None

            raise AssertionError((resource, name))

        delete_project_result = unittest.mock.Mock(
            returncode=0,
            stdout="202",
            stderr="",
        )
        delete_secret_result = unittest.mock.Mock(
            returncode=0,
            stdout='secret "rs1-id-group-secret" deleted',
            stderr="",
        )

        with (
            patch.object(
                ops_manager,
                "list_projects",
                side_effect=[projects_before, projects_after],
            ) as list_mock,
            patch.object(ops_manager.kube, "get_json", side_effect=fake_get),
            patch.object(
                ops_manager.kube,
                "list_json",
                return_value=[{"metadata": {"name": "ops-manager-0"}}],
            ),
            patch.object(
                ops_manager,
                "run_process",
                side_effect=[delete_project_result, delete_secret_result],
            ) as run_mock,
            patch.object(ops_manager.time, "sleep"),
        ):
            ops_manager.delete_project(config, "RS1", timeout=10)

        self.assertEqual(list_mock.call_count, 2)
        self.assertEqual(run_mock.call_count, 2)

        ops_manager_call = run_mock.call_args_list[0]
        curl_config = ops_manager_call.kwargs["input_text"]
        self.assertIn("/api/public/v1.0/groups/rs1-id", curl_config)
        self.assertIn('request = "DELETE"', curl_config)

        secret_call = run_mock.call_args_list[1]
        secret_command = secret_call.args[0]

        self.assertIn("delete", secret_command)
        self.assertIn("secret", secret_command)
        self.assertIn("rs1-id-group-secret", secret_command)
        self.assertIn("--ignore-not-found=true", secret_command)

    def test_ops_manager_delete_project_is_idempotent_when_absent(self) -> None:
        config = {}

        with (
            patch.object(
                ops_manager,
                "list_projects",
                return_value=(
                    "mongodb-development",
                    [{"id": "base-id", "name": "mongodb-development"}],
                ),
            ),
            patch.object(ops_manager, "run_process") as run_mock,
        ):
            ops_manager.delete_project(config, "RS1")

        run_mock.assert_not_called()

    def test_ops_manager_delete_project_refuses_platform_project(self) -> None:
        config = {}

        with (
            patch.object(
                ops_manager,
                "list_projects",
                return_value=(
                    "mongodb-development",
                    [{"id": "base-id", "name": "mongodb-development"}],
                ),
            ),
            patch.object(ops_manager, "run_process") as run_mock,
        ):
            with self.assertRaises(ops_manager.ControllerError) as ctx:
                ops_manager.delete_project(config, "mongodb-development")

        self.assertIn("Refusing to delete permanent", str(ctx.exception))
        run_mock.assert_not_called()

    def test_list_managed_resources_reports_clean_zero_state(self) -> None:
        vault = FakeVault({})

        def fake_list(_config, resource, **kwargs):
            self.assertIn(
                resource,
                {"mongodb", "mongodbuser", "pvc", "pv", "configmap", "secret"},
            )
            if resource == "pv":
                self.assertFalse(kwargs.get("namespaced", True))
            return []

        output = io.StringIO()
        with (
            patch.object(maintenance.kube, "list_json", side_effect=fake_list),
            patch.object(
                maintenance,
                "list_ops_manager_projects",
                return_value=("mongodb-development", []),
            ),
            redirect_stdout(output),
        ):
            maintenance.list_managed_resources(self.config, vault)

        text = output.getvalue()
        self.assertRegex(text, r"(?m)^Managed deployments:\s+0$")
        self.assertRegex(text, r"(?m)^MongoDB resources:\s+0$")
        self.assertRegex(text, r"(?m)^MongoDB users:\s+0$")
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
            if resource == "mongodbuser":
                return [{"metadata": {"name": "tc-sc9-admin"}}]
            if resource == "pvc":
                return [{"metadata": {"name": "data-sc9-0-0"}}]
            if resource == "pv":
                return [{"metadata": {"name": "sc9-shard-0"}}]
            if resource == "configmap":
                return [
                    {"metadata": {"name": "unrelated-config"}},
                    {"metadata": {"name": "tc-deployment-lock-sc9"}},
                ]
            if resource == "secret":
                return []
            raise AssertionError(resource)

        output = io.StringIO()
        with (
            patch.object(maintenance.kube, "list_json", side_effect=fake_list),
            patch.object(
                maintenance,
                "list_ops_manager_projects",
                return_value=(
                    "mongodb-development",
                    [{"id": "sc9-id", "name": "SC9"}],
                ),
            ),
            redirect_stdout(output),
        ):
            maintenance.list_managed_resources(self.config, vault)

        text = output.getvalue()
        self.assertRegex(text, r"(?m)^Managed deployments:\s+1$")
        self.assertRegex(text, r"(?m)^MongoDB resources:\s+1$")
        self.assertRegex(text, r"(?m)^MongoDB users:\s+1$")
        self.assertRegex(text, r"(?m)^PVCs:\s+1$")
        self.assertRegex(text, r"(?m)^PVs:\s+1$")
        self.assertRegex(text, r"(?m)^Deployment locks:\s+1$")
        self.assertIn("Status: ATTENTION REQUIRED", text)
        self.assertIn("SC9", text)
        self.assertIn("sc9", text)
        self.assertIn("tc-sc9-admin", text)
        self.assertIn("data-sc9-0-0", text)
        self.assertIn("sc9-shard-0", text)
        self.assertIn("tc-deployment-lock-sc9", text)
        self.assertNotIn("unrelated-config", text)

    def test_inventory_classifies_platform_state_and_orphan_group_secret(self) -> None:
        vault = FakeVault({})

        def fake_list(_config, resource, **kwargs):
            if resource in {"mongodb", "mongodbuser", "pvc", "pv"}:
                return []
            if resource == "configmap":
                return [{"metadata": {"name": "tc-ops-manager-projects"}}]
            if resource == "secret":
                return [
                    {"metadata": {"name": "tfstate-default-test-state"}},
                    {"metadata": {"name": "stale-project-id-group-secret"}},
                ]
            raise AssertionError(resource)

        with (
            patch.object(maintenance.kube, "list_json", side_effect=fake_list),
            patch.object(
                maintenance,
                "list_ops_manager_projects",
                return_value=(
                    "mongodb-development",
                    [{"id": "base-id", "name": "mongodb-development"}],
                ),
            ),
        ):
            resources = maintenance.managed_resource_inventory(
                self.config,
                vault,
            )

        self.assertEqual(
            resources["controller_infrastructure_configmaps"],
            ["tc-ops-manager-projects"],
        )
        self.assertEqual(
            resources["terraform_states"],
            ["tfstate-default-test-state"],
        )
        self.assertEqual(
            resources["ops_manager_platform_project"],
            ["mongodb-development (Project ID: base-id)"],
        )
        self.assertEqual(
            resources["orphan_group_secrets"],
            [
                "stale-project-id-group-secret "
                "(Project ID: stale-project-id)"
            ],
        )

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

        self.assertIn("live privateWorkerReplacement-managed MongoDB", str(ctx.exception))
        apply_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
