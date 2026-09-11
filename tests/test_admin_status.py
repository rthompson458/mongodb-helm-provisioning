"""Regression tests for administrator managed-resource status presentation."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from privateWorkerReplacement import admin_status
from helpers import FakeVault


def _resources(**overrides: list[str]) -> dict[str, list[str]]:
    """Return a complete empty inventory fixture with permanent infrastructure."""

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
        "deployment_locks": [],
        "ops_manager_projects": [],
        "ops_manager_group_secrets": [],
        "ops_manager_orphans": [],
        "orphan_group_secrets": [],
        "missing_ops_manager_projects": [],
        "missing_ops_manager_platform_project": [],
        "controller_infrastructure_configmaps": ["tc-ops-manager-projects"],
        "terraform_states": ["tfstate-default-mongodb-vault-controller"],
        "ops_manager_platform_project": [
            "mongodb-development (Project ID: base-id)"
        ],
        "ops_manager_platform_group_secrets": [],
    }
    resources.update(overrides)
    return resources


class AdminStatusTests(unittest.TestCase):
    """Verify clean, active, and attention-required inventory presentation."""

    def _capture(self, resources: dict[str, list[str]]) -> str:
        output = io.StringIO()
        with (
            patch.object(
                admin_status,
                "managed_resource_inventory",
                return_value=resources,
            ),
            redirect_stdout(output),
        ):
            admin_status.list_managed_resources({}, FakeVault({}))
        return output.getvalue()

    def test_permanent_infrastructure_only_reports_clean(self) -> None:
        text = self._capture(_resources())

        self.assertIn("Status: CLEAN", text)
        self.assertNotIn("ATTENTION REQUIRED", text)
        self.assertRegex(
            text,
            r"(?m)^Controller infrastructure ConfigMaps:\s+1$",
        )
        self.assertRegex(text, r"(?m)^Terraform backend states:\s+1$")
        self.assertIn(
            "mongodb-development (Project ID: base-id)",
            text,
        )

    def test_empty_detail_categories_are_explicitly_shown_as_none(self) -> None:
        text = self._capture(_resources())

        self.assertIn("Managed deployments:\n  None", text)
        self.assertIn("ReplicaSets:\n  None", text)
        self.assertIn("ShardedClusters:\n  None", text)
        self.assertIn("Databases:\n  None", text)
        self.assertIn("Ops Manager orphan projects:\n  None", text)
        self.assertIn("Ops Manager platform group secrets:\n  None", text)

    def test_nonempty_inventory_reports_neutral_status_and_ids(self) -> None:
        resources = _resources(
            managed_deployments=["RS7"],
            replica_sets=["RS7"],
            databases=["RS7/HouseInfo"],
            managed_accounts=[
                "RS7/HouseInfo/HouseInfo_owner",
                "RS7/HouseInfo/HouseInfo_read",
                "RS7/HouseInfo/HouseInfo_readWrite",
            ],
            mongodb_resources=["rs7"],
            mongodb_users=["tc-rs7-admin"],
            pvcs=["data-rs7-0"],
            pvs=["rs7-0"],
            controller_secrets=["tc-rs7-admin-password"],
            ops_manager_projects=["RS7 (Project ID: rs7-id)"],
            ops_manager_group_secrets=[
                "rs7-id-group-secret (Project ID: rs7-id)"
            ],
        )

        text = self._capture(resources)

        self.assertIn("Status: MANAGED RESOURCES PRESENT", text)
        self.assertNotIn("ATTENTION REQUIRED", text)
        self.assertIn("RS7 (Project ID: rs7-id)", text)
        self.assertIn(
            "rs7-id-group-secret (Project ID: rs7-id)",
            text,
        )
        self.assertIn("DBaaS PVCs:", text)
        self.assertIn("DBaaS PVs:", text)

    def test_orphan_ops_manager_project_requires_attention(self) -> None:
        text = self._capture(
            _resources(
                ops_manager_orphans=[
                    "wildcats (Project ID: wildcats-id)"
                ],
            )
        )

        self.assertIn("Status: ATTENTION REQUIRED", text)
        self.assertIn("Ops Manager orphan projects:", text)
        self.assertIn("wildcats (Project ID: wildcats-id)", text)

    def test_orphan_group_secret_requires_attention_and_shows_project_id(self) -> None:
        text = self._capture(
            _resources(
                orphan_group_secrets=[
                    "stale-id-group-secret (Project ID: stale-id)"
                ],
            )
        )

        self.assertIn("Status: ATTENTION REQUIRED", text)
        self.assertRegex(text, r"(?m)^Orphan group secrets:\s+1$")
        self.assertIn(
            "stale-id-group-secret (Project ID: stale-id)",
            text,
        )

    def test_missing_platform_project_requires_attention(self) -> None:
        text = self._capture(
            _resources(
                ops_manager_platform_project=[
                    "mongodb-development (Project ID: NOT FOUND)"
                ],
                missing_ops_manager_platform_project=["mongodb-development"],
            )
        )

        self.assertIn("Status: ATTENTION REQUIRED", text)
        self.assertRegex(
            text,
            r"(?m)^Missing Ops Manager platform project:\s+1$",
        )
        self.assertIn("Project ID: NOT FOUND", text)


if __name__ == "__main__":
    unittest.main()
