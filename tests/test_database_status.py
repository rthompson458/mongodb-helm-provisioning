"""Regression tests for database-level status and account detail separation."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from terraform_controller import cli, database_status
from helpers import FakeVault, deployment_inventory


class DatabaseStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            "config_path": str(Path(self.temp.name) / "terraformController.config"),
            "rotation_days": 30,
            "vault_address": "http://127.0.0.1:8200",
            "vault_mount": "secret",
            "vault_base_path": "mongodb",
        }

    def _capture(self, function, *args) -> str:
        stream = io.StringIO()
        with redirect_stdout(stream):
            function(*args)
        return stream.getvalue()

    def test_list_databases_shows_database_status_not_accounts(self) -> None:
        vault = FakeVault(deployment_inventory(with_db=True))
        with (
            patch.object(database_status, "list_operation_records", return_value=[]),
            patch.object(database_status.kube, "phase", return_value="Running"),
        ):
            text = self._capture(database_status.list_databases, self.config, vault)

        self.assertIn("DEPLOYMENT", text)
        self.assertIn("DATABASE", text)
        self.assertIn("STATUS", text)
        self.assertIn("HouseInfo", text)
        self.assertIn("Ready", text)
        self.assertNotIn("HouseInfo_owner", text)
        self.assertNotIn("ROTATES IN", text)

    def test_list_database_shows_unavailable_when_parent_is_not_running(self) -> None:
        vault = FakeVault(deployment_inventory(with_db=True))
        with (
            patch.object(database_status, "list_operation_records", return_value=[]),
            patch.object(database_status.kube, "phase", return_value="Pending"),
        ):
            text = self._capture(
                database_status.list_database,
                self.config,
                vault,
                "RS1",
                "HouseInfo",
            )

        self.assertIn("Database:        HouseInfo", text)
        self.assertIn("Status:          Unavailable", text)
        self.assertNotIn("HouseInfo_owner", text)

    def test_active_add_database_is_visible_as_creating_before_inventory_commit(self) -> None:
        vault = FakeVault(deployment_inventory())
        record = {
            "command": "AddDatabase",
            "deployment": "RS1",
            "result": "In Progress",
            "pid": None,
            "worker_arguments": ["AddDatabase", "RS1", "NewData"],
        }
        with (
            patch.object(database_status, "list_operation_records", return_value=[record]),
            patch.object(database_status, "effective_result", return_value="In Progress"),
            patch.object(database_status.kube, "phase", return_value="Running"),
        ):
            text = self._capture(database_status.list_databases, self.config, vault)

        self.assertIn("newdata", text)
        self.assertIn("Creating", text)

    def test_active_delete_database_is_reported_as_deleting(self) -> None:
        vault = FakeVault(deployment_inventory(with_db=True))
        record = {
            "command": "DeleteDatabase",
            "deployment": "RS1",
            "result": "In Progress",
            "pid": None,
            "worker_arguments": ["DeleteDatabase", "RS1", "HouseInfo", "--confirm"],
        }
        with (
            patch.object(database_status, "list_operation_records", return_value=[record]),
            patch.object(database_status, "effective_result", return_value="In Progress"),
            patch.object(database_status.kube, "phase", return_value="Running"),
        ):
            text = self._capture(database_status.list_databases, self.config, vault)

        self.assertIn("HouseInfo", text)
        self.assertIn("Deleting", text)

    def test_list_database_accounts_owns_rotation_and_vault_details(self) -> None:
        vault = FakeVault(deployment_inventory(with_db=True))
        text = self._capture(
            database_status.list_database_accounts,
            self.config,
            vault,
            "RS1",
            "HouseInfo",
        )

        self.assertIn("HouseInfo_owner", text)
        self.assertIn("ROTATES IN", text)
        self.assertIn("Last rotated:", text)
        self.assertIn(
            "http://127.0.0.1:8200/ui/vault/secrets/secret/show/",
            text,
        )

    def test_public_parser_exposes_list_database_accounts(self) -> None:
        args = cli.build_parser().parse_args(
            ["ListDatabaseAccounts", "RS1", "HouseInfo"]
        )
        self.assertEqual(args.command, "ListDatabaseAccounts")
        self.assertEqual(args.deployment_or_database, "RS1")
        self.assertEqual(args.database, "HouseInfo")

    def test_public_main_without_command_prints_help_and_succeeds(self) -> None:
        text = self._capture(cli.main, [])
        self.assertIn("Terraform-driven MongoDB DBaaS controller", text)
        self.assertIn("ListDatabaseAccounts", text)


if __name__ == "__main__":
    unittest.main()
