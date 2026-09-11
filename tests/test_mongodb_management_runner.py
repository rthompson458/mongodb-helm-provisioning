"""Regression tests for the Terraform-to-Helm database-management handoff.

These tests verify the Python runner passes the configuration Terraform needs
for the integrated MongoDB management Helm chart without requiring a live
Kubernetes, MongoDB, Vault, or Helm environment.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from privateWorkerReplacement import terraform_runner


class MongoDBManagementRunnerTests(unittest.TestCase):
    """Verify Helm-management Terraform inputs created by the Python runner."""

    def _config(self, root: Path) -> dict:
        """Return the minimum complete runner configuration for these tests."""

        return {
            "terraform_source": root / "terraform-dbaas",
            "terraform_cache": root / "cache",
            "vault_token_env": "TEST_VAULT_TOKEN",
            "vault_address": "http://127.0.0.1:8200",
            "vault_mount": "secret",
            "vault_base_path": "mongodb",
            "rotation_days": 30,
            "mongodb_namespace": "mongodb",
            "ops_manager_config_map": "my-project",
            "ops_manager_credentials_secret": "organization-secret",
            "mongodb_auth_database": "admin",
            "kubeconfig": "/tmp/fake-kubeconfig",
            "kube_context": "k3d-test",
            "mongo_image": "mongo:8.0",
            "placeholder_collection": "__dbaas_metadata",
            "job_timeout": 600,
            "default_members": 3,
            "storage_class": "local-path",
            "storage_size": "16Gi",
            "storage_base_path": "/tmp/storage",
            "storage_node_name": "k3d-test-server-0",
            "backend_secret_suffix": "test-state",
            "backend_namespace": "terraform",
        }

    def test_runner_passes_management_timeout_to_terraform(self) -> None:
        """Runtime job timeout must also control the Helm release timeout."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            captured_env: dict[str, str] = {}

            def fake_run_diagnostic(
                _config,
                command,
                *,
                cwd=None,
                env=None,
                label,
            ):
                if command[:2] == ["terraform", "apply"]:
                    captured_env.update(env or {})

            with (
                patch.object(
                    terraform_runner,
                    "_sync",
                    return_value=root,
                ),
                patch.object(
                    terraform_runner,
                    "_run_diagnostic",
                    side_effect=fake_run_diagnostic,
                ),
                patch.object(
                    terraform_runner,
                    "log_event",
                ),
                patch.dict(
                    os.environ,
                    {"TEST_VAULT_TOKEN": "test-token"},
                    clear=False,
                ),
            ):
                terraform_runner._apply_inventory_locked(
                    config,
                    {},
                    terraform_runner._operation_payload(None),
                )

            self.assertEqual(
                captured_env[
                    "TF_VAR_mongodb_management_timeout_seconds"
                ],
                "600",
            )

    def test_delete_database_enables_destructive_operation_gate(self) -> None:
        """An approved DeleteDatabase operation must enable Terraform's gate."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            captured_payload: dict = {}

            operation = terraform_runner._operation_payload(
                {
                    "action": "delete_database",
                    "deployment": "rs1",
                    "deployment_type": "ReplicaSet",
                    "database": "HouseInfo",
                    "operation_id": "delete-test-001",
                }
            )

            def fake_run_diagnostic(
                _config,
                command,
                *,
                cwd=None,
                env=None,
                label,
            ):
                if command[:2] != ["terraform", "apply"]:
                    return

                var_file_argument = next(
                    argument
                    for argument in command
                    if argument.startswith("-var-file=")
                )

                var_file = Path(
                    var_file_argument.split("=", 1)[1]
                )

                # terraform_runner intentionally passes the temporary tfvars
                # filename relative to Terraform's working directory. Resolve
                # it the same way Terraform does before reading it here.
                if not var_file.is_absolute():
                    if cwd is None:
                        raise AssertionError(
                            "Terraform apply did not provide a working directory."
                        )
                    var_file = Path(cwd) / var_file

                captured_payload.update(
                    json.loads(
                        var_file.read_text(
                            encoding="utf-8"
                        )
                    )
                )

            with (
                patch.object(
                    terraform_runner,
                    "_sync",
                    return_value=root,
                ),
                patch.object(
                    terraform_runner,
                    "_run_diagnostic",
                    side_effect=fake_run_diagnostic,
                ),
                patch.object(
                    terraform_runner,
                    "log_event",
                ),
                patch.dict(
                    os.environ,
                    {"TEST_VAULT_TOKEN": "test-token"},
                    clear=False,
                ),
            ):
                terraform_runner._apply_inventory_locked(
                    config,
                    {},
                    operation,
                )

            self.assertTrue(
                captured_payload[
                    "allow_destructive_mongodb_operations"
                ]
            )
            self.assertEqual(
                captured_payload["operation"]["action"],
                "delete_database",
            )
            self.assertEqual(
                captured_payload["operation"]["deployment"],
                "rs1",
            )
            self.assertEqual(
                captured_payload["operation"]["database"],
                "HouseInfo",
            )


if __name__ == "__main__":
    unittest.main()
