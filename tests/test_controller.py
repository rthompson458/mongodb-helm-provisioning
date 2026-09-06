from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from terraform_controller import controller


def rs_inventory(*, with_db: bool = False, owner_disabled: bool = False):
    databases = {}
    if with_db:
        databases["houseinfo"] = {
            "display_name": "HouseInfo",
            "created_at": "2026-08-01T12:00:00Z",
            "owner_disabled": owner_disabled,
            "owner_disabled_at": "2026-08-31T12:00:00Z" if owner_disabled else "",
            "rotation_version": 1,
            "rotated_at": "2026-08-01T12:00:00Z",
        }
    return {
        "rs1": {
            "display_name": "RS1",
            "created_at": "2026-08-01T11:00:00Z",
            "members": 3,
            "version": "8.0.29",
            "persistent": True,
            "storage_class": "mongodb-data-local",
            "storage_size": "16Gi",
            "controller_password_version": 1,
            "databases": databases,
        }
    }


class FakeVault:
    def __init__(self, inventory):
        self.inventory = inventory

    def load_inventory(self):
        return self.inventory


class ControllerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "rotation_days": 30,
            "rs_ready_timeout": 30,
            "vault_address": "http://127.0.0.1:8200",
            "vault_base_path": "mongodb",
        }

    def test_add_database_materializes_before_accounts_are_added(self):
        vault = FakeVault(rs_inventory())
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append((copy.deepcopy(inventory), copy.deepcopy(operation)))

        with (
            patch.object(controller.kube, "phase", return_value="Running"),
            patch.object(controller, "apply_inventory", side_effect=apply_side_effect),
            patch.object(controller, "_verify_database_accounts"),
            patch.object(controller, "utc_now"),
        ):
            controller.utc_now.return_value = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
            controller.add_database(self.config, vault, "RS1", "HouseInfo")

        self.assertEqual(calls[0][1]["action"], "create_database")
        self.assertEqual(calls[0][0]["rs1"]["databases"], {})
        self.assertIsNone(calls[1][1])
        self.assertIn("houseinfo", calls[1][0]["rs1"]["databases"])

    def test_rotate_passwords_is_requested_as_terraform_operation(self):
        vault = FakeVault(rs_inventory(with_db=True))
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
            patch.object(controller.kube, "phase", return_value="Running"),
            patch.object(controller, "apply_inventory", side_effect=apply_side_effect),
            patch.object(controller, "_verify_database_accounts"),
        ):
            controller.rotate_passwords(self.config, vault, "RS1", "HouseInfo")

        self.assertEqual(calls[0][1]["action"], "rotate_passwords")
        self.assertEqual(calls[0][0]["rs1"]["databases"]["houseinfo"]["rotation_version"], 1)
        self.assertFalse(calls[0][0]["rs1"]["databases"]["houseinfo"]["owner_disabled"])

    def test_rotate_passwords_recovers_with_a_new_revision_after_partial_apply(self):
        vault = FakeVault(rs_inventory(with_db=True))
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append((copy.deepcopy(inventory), copy.deepcopy(operation)))
            db = vault.inventory["rs1"]["databases"]["houseinfo"]
            if len(calls) == 1:
                # Model Terraform committing metadata before one of the two
                # write-only password sinks fails.
                db["rotation_version"] = 2
                db["rotated_at"] = "2026-09-06T12:00:00Z"
                raise controller.ControllerError("simulated partial apply")
            db["rotation_version"] = 3
            db["rotated_at"] = "2026-09-06T12:01:00Z"

        with (
            patch.object(controller.kube, "phase", return_value="Running"),
            patch.object(controller, "apply_inventory", side_effect=apply_side_effect),
            patch.object(controller, "_verify_database_accounts"),
        ):
            controller.rotate_passwords(self.config, vault, "RS1", "HouseInfo")

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0]["rs1"]["databases"]["houseinfo"]["rotation_version"], 1)
        self.assertEqual(calls[1][0]["rs1"]["databases"]["houseinfo"]["rotation_version"], 2)
        self.assertEqual(calls[1][1]["action"], "rotate_passwords")

    def test_rotate_passwords_requires_running_replica_set(self):
        vault = FakeVault(rs_inventory(with_db=True))
        with (
            patch.object(controller.kube, "phase", return_value="Pending"),
            patch.object(controller, "apply_inventory") as apply_mock,
        ):
            with self.assertRaises(controller.ControllerError):
                controller.rotate_passwords(self.config, vault, "RS1", "HouseInfo")
        apply_mock.assert_not_called()

    def test_delete_database_drops_then_removes_desired_state(self):
        vault = FakeVault(rs_inventory(with_db=True))
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append((copy.deepcopy(inventory), copy.deepcopy(operation)))

        with (
            patch.object(controller.kube, "phase", return_value="Running"),
            patch.object(controller.kube, "wait_absent"),
            patch.object(controller, "apply_inventory", side_effect=apply_side_effect),
        ):
            controller.delete_database(self.config, vault, "RS1", "HouseInfo", True)

        self.assertEqual(calls[0][1]["action"], "delete_database")
        self.assertIn("houseinfo", calls[0][0]["rs1"]["databases"])
        self.assertIsNone(calls[1][1])
        self.assertNotIn("houseinfo", calls[1][0]["rs1"]["databases"])
        self.assertEqual(calls[2][1]["action"], "verify_database_users_absent")

    def test_disable_owner_is_requested_as_terraform_operation(self):
        vault = FakeVault(rs_inventory(with_db=True))
        calls = []

        def apply_side_effect(config, inventory, operation=None):
            calls.append((copy.deepcopy(inventory), copy.deepcopy(operation)))
            if operation and operation.get("action") == "disable_owner":
                db = vault.inventory["rs1"]["databases"]["houseinfo"]
                db["owner_disabled"] = True
                db["owner_disabled_at"] = "2026-09-06T12:00:00Z"

        with (
            patch.object(controller.kube, "phase", return_value="Running"),
            patch.object(controller, "apply_inventory", side_effect=apply_side_effect),
            patch.object(controller, "_verify_database_accounts"),
        ):
            controller.disable_owner(self.config, vault, "RS1", "HouseInfo", True)

        self.assertEqual(calls[0][1]["action"], "disable_owner")
        self.assertFalse(calls[0][0]["rs1"]["databases"]["houseinfo"]["owner_disabled"])


if __name__ == "__main__":
    unittest.main()
