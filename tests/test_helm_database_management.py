"""Regression tests for Terraform/Helm MongoDB database management wiring."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


class HelmDatabaseManagementTests(unittest.TestCase):
    """Keep the integrated Helm jobs aligned with the controller contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parent.parent
        cls.tf_root = cls.repo_root / "terraform-dbaas"
        cls.main_text = (cls.tf_root / "main.tf").read_text(encoding="utf-8")
        cls.variables_text = (cls.tf_root / "variables.tf").read_text(
            encoding="utf-8"
        )
        cls.versions_text = (cls.tf_root / "versions.tf").read_text(
            encoding="utf-8"
        )
        cls.lifecycle_text = (
            cls.tf_root / "scripts" / "lifecycle.sh"
        ).read_text(encoding="utf-8")
        cls.db_job_text = (
            cls.tf_root
            / "mongodb-chart"
            / "templates"
            / "mongodb-database-job.yaml"
        ).read_text(encoding="utf-8")
        cls.operation_job_text = (
            cls.tf_root
            / "mongodb-chart"
            / "templates"
            / "mongodb-database-operation-job.yaml"
        ).read_text(encoding="utf-8")

    def test_expected_helm_chart_files_exist(self) -> None:
        chart = self.tf_root / "mongodb-chart"
        expected = [
            chart / "Chart.yaml",
            chart / "values.yaml",
            chart / "templates" / "mongodb-database-job.yaml",
            chart / "templates" / "mongodb-database-operation-job.yaml",
        ]
        for path in expected:
            self.assertTrue(path.is_file(), f"Missing Helm chart file: {path}")

    def test_database_lifecycle_actions_are_routed_only_through_helm(self) -> None:
        self.assertIn('resource "helm_release" "mongodb_management"', self.main_text)
        action_match = re.search(
            r"mongodb_database_actions\s*=\s*\[(.*?)\]",
            self.main_text,
            re.DOTALL,
        )
        self.assertIsNotNone(action_match)
        actions = set(re.findall(r'"([^"]+)"', action_match.group(1)))
        self.assertEqual(
            actions,
            {"create_database", "delete_database", "validate_deployment_empty"},
        )

        for shell_case in (
            "  create_database)",
            "  delete_database)",
            "  validate_deployment_empty)",
        ):
            self.assertNotIn(shell_case, self.lifecycle_text)

    def test_management_names_remain_in_integrated_contract(self) -> None:
        self.assertIn("mongodbDatabases", self.main_text)
        self.assertIn("mongodbDatabaseOperations", self.main_text)
        self.assertIn(
            'variable "mongodb_management_timeout_seconds"',
            self.variables_text,
        )
        self.assertIn('version = "3.3.0"', self.versions_text)

    def test_database_account_model_remains_three_accounts_per_database(self) -> None:
        self.assertIn('suffix = "owner"', self.main_text)
        self.assertIn('suffix = "readWrite"', self.main_text)
        self.assertIn('suffix = "read"', self.main_text)
        self.assertIn(
            'username         = "${database.database_name}_${account_type.suffix}"',
            self.main_text,
        )
        self.assertIn('role   = "dbOwner"', self.main_text)
        self.assertIn('role   = "readWrite"', self.main_text)
        self.assertIn('role   = "read"', self.main_text)

    def test_helm_jobs_preserve_database_safety_and_idempotency(self) -> None:
        self.assertIn("database.createCollection(collection)", self.db_job_text)
        self.assertIn('["admin", "config", "local"]', self.db_job_text)

        for action in (
            "validateDeploymentEmpty",
            "renameCollection",
            "deleteCollection",
            "deleteDatabase",
        ):
            self.assertIn(action, self.operation_job_text)

        self.assertIn('["admin", "config", "local"]', self.operation_job_text)
        self.assertIn("the collection is not empty", self.operation_job_text)
        self.assertIn("Already absent database", self.operation_job_text)


if __name__ == "__main__":
    unittest.main()
