"""Static contract tests for the integrated MongoDB management Helm chart."""

from __future__ import annotations

import unittest
from pathlib import Path


class MongoDBManagementChartTests(unittest.TestCase):
    """Keep the integrated Helm management path aligned with the controller model."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parent.parent
        cls.tf_root = cls.repo_root / "terraform-dbaas"
        cls.main_text = (cls.tf_root / "main.tf").read_text(encoding="utf-8")
        cls.variables_text = (cls.tf_root / "variables.tf").read_text(encoding="utf-8")
        cls.runner_text = (
            cls.repo_root / "privateWorkerReplacement" / "terraform_runner.py"
        ).read_text(encoding="utf-8")
        cls.database_job = (
            cls.tf_root
            / "mongodb-chart"
            / "templates"
            / "mongodb-database-job.yaml"
        ).read_text(encoding="utf-8")
        cls.operation_job = (
            cls.tf_root
            / "mongodb-chart"
            / "templates"
            / "mongodb-database-operation-job.yaml"
        ).read_text(encoding="utf-8")

    def test_chart_is_a_real_terraform_lifecycle_component(self) -> None:
        self.assertIn('resource "helm_release" "mongodb_management"', self.main_text)
        self.assertIn('chart     = "${path.module}/mongodb-chart"', self.main_text)
        self.assertIn("mongodbDatabases", self.main_text)
        self.assertIn("mongodbDatabaseOperations", self.main_text)

    def test_database_identity_remains_deployment_and_database_scoped(self) -> None:
        self.assertIn("local.mongodb_databases", self.main_text)
        self.assertNotIn("mission_name", self.main_text)
        self.assertNotIn("mission_name", self.variables_text)
        self.assertIn(
            "<Database>_owner",
            (self.repo_root / "README.md").read_text(encoding="utf-8"),
        )

    def test_create_and_delete_database_are_owned_by_helm_not_lifecycle_shell(self) -> None:
        lifecycle = (self.tf_root / "scripts" / "lifecycle.sh").read_text(encoding="utf-8")
        self.assertNotIn("  create_database)", lifecycle)
        self.assertNotIn("  delete_database)", lifecycle)
        self.assertIn('"delete_database"', self.main_text)
        self.assertIn('"deleteDatabase"', self.main_text)

    def test_destructive_operation_gate_is_carried_from_confirmed_cli_operation(self) -> None:
        self.assertIn(
            'variable "allow_destructive_mongodb_operations"',
            self.variables_text,
        )
        self.assertIn(
            '"allow_destructive_mongodb_operations": op["action"] == "delete_database"',
            self.runner_text,
        )
        self.assertIn("allow_destructive_mongodb_operations", self.main_text)

    def test_hook_order_and_protected_database_safeguards_are_preserved(self) -> None:
        self.assertIn('"helm.sh/hook-weight": "-10"', self.operation_job)
        self.assertIn('"helm.sh/hook-weight": "10"', self.database_job)
        self.assertIn('["admin", "config", "local"]', self.operation_job)
        self.assertIn("the collection is not empty", self.operation_job)
        self.assertIn("Already absent database", self.operation_job)


if __name__ == "__main__":
    unittest.main()
