"""Unit tests for cleanup of MongoDB Operator and Helm runtime artifacts."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from privateWorkerReplacement import operator_artifacts


REPO_ROOT = Path(__file__).resolve().parent.parent


class OperatorArtifactTests(unittest.TestCase):
    """Verify narrow cleanup and recovery discovery for non-Terraform objects."""

    def setUp(self) -> None:
        self.config = {
            "kubeconfig": "/tmp/fake-kubeconfig",
            "kube_context": "k3d-test",
            "mongodb_namespace": "mongodb",
        }

    def test_expected_artifact_names_match_chart_and_operator_conventions(self) -> None:
        artifacts = operator_artifacts.deployment_operator_artifact_names("rs1")

        self.assertEqual(
            artifacts["job"],
            [
                "rs1-mongodb-management-database-provisioner",
                "rs1-mongodb-management-database-operation",
            ],
        )
        self.assertEqual(
            artifacts["secret"],
            ["rs1-agent-auth-secret"],
        )

    def test_cleanup_refuses_while_mongodb_resource_is_live(self) -> None:
        with (
            mock.patch.object(
                operator_artifacts.kube,
                "get_json",
                return_value={"metadata": {"name": "rs1"}},
            ),
            mock.patch.object(operator_artifacts, "run_process") as run_mock,
        ):
            with self.assertRaises(operator_artifacts.ControllerError):
                operator_artifacts.cleanup_deployment_operator_artifacts(
                    self.config,
                    "rs1",
                )

        run_mock.assert_not_called()

    def test_cleanup_deletes_jobs_with_pods_and_agent_auth_secret(self) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(
                operator_artifacts.kube,
                "get_json",
                return_value=None,
            ),
            mock.patch.object(
                operator_artifacts.kube,
                "list_json",
                return_value=[],
            ) as list_mock,
            mock.patch.object(
                operator_artifacts,
                "run_process",
                return_value=completed,
            ) as run_mock,
            mock.patch.object(operator_artifacts, "log_event"),
        ):
            operator_artifacts.cleanup_deployment_operator_artifacts(
                self.config,
                "rs1",
            )

        self.assertEqual(run_mock.call_count, 3)
        job_commands = [
            call.args[0]
            for call in run_mock.call_args_list[:2]
        ]
        for command in job_commands:
            self.assertIn("--cascade=foreground", command)
            self.assertIn("--wait=true", command)

        secret_command = run_mock.call_args_list[2].args[0]
        self.assertNotIn("--cascade=foreground", secret_command)
        self.assertIn("rs1-agent-auth-secret", secret_command)

        self.assertEqual(list_mock.call_count, 2)
        selectors = [
            call.kwargs["label_selector"]
            for call in list_mock.call_args_list
        ]
        self.assertEqual(
            selectors,
            [
                "job-name=rs1-mongodb-management-database-provisioner",
                "job-name=rs1-mongodb-management-database-operation",
            ],
        )

    def test_discovery_uses_managed_labels_and_orphan_artifact_names(self) -> None:
        def fake_list(_config, resource, **kwargs):
            if resource == "mongodbuser":
                return [
                    {
                        "metadata": {
                            "name": "tc-rs1-admin",
                            "labels": {
                                "app.kubernetes.io/managed-by": (
                                    "privateWorkerReplacement"
                                ),
                                "dbaas.replica-set": "rs1",
                            },
                        }
                    }
                ]
            if resource in {"configmap", "pvc", "pv"}:
                return []
            if resource == "job":
                return [
                    {
                        "metadata": {
                            "name": (
                                "oldrs-mongodb-management-"
                                "database-provisioner"
                            ),
                            "labels": {},
                        }
                    }
                ]
            if resource == "pod":
                return [
                    {
                        "metadata": {
                            "name": "oldrs-hook-pod-abcde",
                            "labels": {
                                "job-name": (
                                    "oldrs-mongodb-management-"
                                    "database-provisioner"
                                )
                            },
                        }
                    }
                ]
            if resource == "secret":
                # The first call uses the managed-by selector. The second is the
                # recovery fallback that scans for Operator-created leftovers.
                if kwargs.get("label_selector"):
                    return []
                return [
                    {
                        "metadata": {
                            "name": "orphanrs-agent-auth-secret",
                            "labels": {},
                        }
                    }
                ]
            raise AssertionError(resource)

        with mock.patch.object(
            operator_artifacts.kube,
            "list_json",
            side_effect=fake_list,
        ):
            keys = operator_artifacts.discover_managed_deployment_keys(
                self.config
            )

        self.assertEqual(keys, ["oldrs", "orphanrs", "rs1"])

    def test_helm_hooks_delete_successful_jobs_and_carry_ownership_labels(self) -> None:
        templates = (
            "mongodb-database-job.yaml",
            "mongodb-database-operation-job.yaml",
        )

        for template in templates:
            with self.subTest(template=template):
                text = (
                    REPO_ROOT
                    / "terraform-dbaas"
                    / "mongodb-chart"
                    / "templates"
                    / template
                ).read_text(encoding="utf-8")
                self.assertIn(
                    '"helm.sh/hook-delete-policy": '
                    "before-hook-creation,hook-succeeded",
                    text,
                )
                self.assertIn(
                    "app.kubernetes.io/managed-by: privateWorkerReplacement",
                    text,
                )
                self.assertIn("dbaas.deployment:", text)


if __name__ == "__main__":
    unittest.main()
