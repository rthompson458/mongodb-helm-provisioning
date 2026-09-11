"""Regression tests for administrator managed-resource status presentation."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from privateWorkerReplacement import admin_status
from helpers import FakeVault


class AdminStatusTests(unittest.TestCase):
    """Resource presence is inventory information, not a health alarm."""

    def _capture(self, resources: dict[str, list[str]]) -> str:
        output = io.StringIO()
        with (
            patch.object(admin_status, "managed_resource_inventory", return_value=resources),
            redirect_stdout(output),
        ):
            admin_status.list_managed_resources({}, FakeVault({}))
        return output.getvalue()

    def test_empty_inventory_reports_clean(self) -> None:
        resources = {
            "managed_deployments": [],
            "replica_sets": [],
            "sharded_clusters": [],
            "databases": [],
            "managed_accounts": [],
            "mongodb_resources": [],
            "mongodb_users": [],
            "pvcs": [],
            "pvs": [],
            "controller_secrets": [],
            "controller_configmaps": [],
            "terraform_states": [],
            "deployment_locks": [],
            "ops_manager_platform_project": ["mongodb-development"],
            "ops_manager_projects": [],
            "ops_manager_orphans": [],
            "missing_ops_manager_projects": [],
        }

        text = self._capture(resources)

        self.assertIn("Status: CLEAN", text)
        self.assertNotIn("ATTENTION REQUIRED", text)

    def test_nonempty_inventory_reports_neutral_status_and_names(self) -> None:
        resources = {
            "managed_deployments": ["RS7"],
            "replica_sets": ["RS7"],
            "sharded_clusters": [],
            "databases": ["RS7/HouseInfo"],
            "managed_accounts": [
                "RS7/HouseInfo/HouseInfo_owner",
                "RS7/HouseInfo/HouseInfo_read",
                "RS7/HouseInfo/HouseInfo_readWrite",
            ],
            "mongodb_resources": ["rs7"],
            "mongodb_users": ["tc-rs7-admin"],
            "pvcs": ["data-rs7-0"],
            "pvs": ["rs7-0"],
            "controller_secrets": ["tc-rs7-admin-password"],
            "controller_configmaps": [],
            "terraform_states": ["tfstate-test-state"],
            "deployment_locks": [],
            "ops_manager_platform_project": ["mongodb-development"],
            "ops_manager_projects": ["RS7"],
            "ops_manager_orphans": [],
            "missing_ops_manager_projects": [],
        }

        text = self._capture(resources)

        self.assertIn("Status: MANAGED RESOURCES PRESENT", text)
        self.assertNotIn("ATTENTION REQUIRED", text)
        self.assertIn("RS7", text)
        self.assertIn("tc-rs7-admin", text)
        self.assertIn("DBaaS PVCs:", text)
        self.assertIn("DBaaS PVs:", text)
        self.assertIn("data-rs7-0", text)
        self.assertIn("rs7-0", text)


    def test_orphan_ops_manager_project_requires_attention(self) -> None:
        resources = {
            "managed_deployments": [],
            "replica_sets": [],
            "sharded_clusters": [],
            "databases": [],
            "managed_accounts": [],
            "mongodb_resources": [],
            "mongodb_users": [],
            "pvcs": [],
            "pvs": [],
            "controller_secrets": [],
            "controller_configmaps": [],
            "terraform_states": [],
            "deployment_locks": [],
            "ops_manager_platform_project": ["mongodb-development"],
            "ops_manager_projects": ["wildcats"],
            "ops_manager_orphans": ["wildcats"],
            "missing_ops_manager_projects": [],
        }

        text = self._capture(resources)

        self.assertIn("Status: ATTENTION REQUIRED", text)
        self.assertIn("Ops Manager orphan projects:", text)
        self.assertIn("wildcats", text)



if __name__ == "__main__":
    unittest.main()
