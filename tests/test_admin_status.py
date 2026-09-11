"""Regression tests for administrator managed-resource status presentation."""

from __future__ import annotations

import io
import re
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
    """Verify compact, verbose, active, and attention-required presentation."""

    def _capture(
        self,
        resources: dict[str, list[str]],
        *,
        verbose: bool = False,
    ) -> str:
        """Render ListManagedResources against one controlled inventory fixture."""

        output = io.StringIO()
        with (
            patch.object(
                admin_status,
                "managed_resource_inventory",
                return_value=resources,
            ),
            redirect_stdout(output),
        ):
            admin_status.list_managed_resources(
                {},
                FakeVault({}),
                verbose=verbose,
            )
        return output.getvalue()

    def test_permanent_infrastructure_only_reports_clean(self) -> None:
        """Permanent platform objects remain visible without making state dirty."""

        text = self._capture(_resources())

        self.assertIn("Status: CLEAN", text)
        self.assertIn("Managed Resources", text)
        self.assertIn("Platform Resources", text)
        self.assertIn("Health / Consistency", text)
        self.assertRegex(
            text,
            r"(?m)^Controller infrastructure ConfigMaps:\s+1$",
        )
        self.assertRegex(text, r"(?m)^Terraform backend states:\s+1$")
        self.assertNotIn("Project ID: base-id", text)

    def test_default_view_is_compact_and_still_shows_zero_health_counts(self) -> None:
        """Normal output must expose all important zero-state checks without raw dumps."""

        text = self._capture(_resources())

        self.assertRegex(text, r"(?m)^Deployments:\s+0$")
        self.assertRegex(text, r"(?m)^  ReplicaSets:\s+0$")
        self.assertRegex(text, r"(?m)^  ShardedClusters:\s+0$")
        self.assertRegex(text, r"(?m)^Databases:\s+0$")
        self.assertRegex(text, r"(?m)^Ops Manager orphan projects:\s+0$")
        self.assertRegex(text, r"(?m)^Orphan group secrets:\s+0$")
        self.assertNotIn("Full Resource Details", text)
        self.assertNotIn("None", text)

    def test_nonempty_inventory_reports_deployment_ownership_summary(self) -> None:
        """Default output should summarize deployment resources without raw object lists."""

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
        self.assertRegex(text, r"(?m)^DBaaS PVCs / PVs:\s+1 / 1$")
        self.assertIn("Deployment Details", text)
        self.assertRegex(
            text,
            r"(?m)^RS7\s+ReplicaSet\s+rs7\s+1\s+1\s+1$",
        )
        self.assertNotIn("Project ID: rs7-id", text)
        self.assertNotIn("data-rs7-0", text)

    def test_deployment_detail_counts_rs_and_sharded_cluster_storage(self) -> None:
        """PVC/PV ownership counts must stay correct for mixed deployment types."""

        resources = _resources(
            managed_deployments=["RS1", "SC2"],
            replica_sets=["RS1"],
            sharded_clusters=["SC2"],
            mongodb_resources=["rs1", "sc2"],
            pvcs=[
                "data-rs1-0",
                "data-rs1-1",
                "data-rs1-2",
                "data-sc2-0-0",
                "data-sc2-0-1",
                "data-sc2-0-2",
                "data-sc2-config-0",
                "data-sc2-config-1",
                "data-sc2-config-2",
            ],
            pvs=[
                "rs1-0",
                "rs1-1",
                "rs1-2",
                "sc2-shard-0",
                "sc2-shard-1",
                "sc2-shard-2",
                "sc2-config-0",
                "sc2-config-1",
                "sc2-config-2",
            ],
        )

        text = self._capture(resources)

        self.assertRegex(text, r"(?m)^RS1\s+ReplicaSet\s+rs1\s+3\s+3\s+0$")
        self.assertRegex(
            text,
            r"(?m)^SC2\s+ShardedCluster\s+sc2\s+6\s+6\s+0$",
        )

    def test_verbose_appends_full_object_names_and_project_ids(self) -> None:
        """Forensic mode should retain the exact low-level inventory when requested."""

        resources = _resources(
            managed_deployments=["RS7"],
            replica_sets=["RS7"],
            mongodb_resources=["rs7"],
            pvcs=["data-rs7-0"],
            pvs=["rs7-0"],
            ops_manager_projects=["RS7 (Project ID: rs7-id)"],
        )

        text = self._capture(resources, verbose=True)

        self.assertIn("Full Resource Details", text)
        self.assertIn("Managed deployments: RS7", text)
        self.assertIn("DBaaS PVCs: data-rs7-0", text)
        self.assertIn("DBaaS PVs: rs7-0", text)
        self.assertIn("RS7 (Project ID: rs7-id)", text)
        self.assertIn("mongodb-development (Project ID: base-id)", text)

    def test_orphan_ops_manager_project_requires_attention(self) -> None:
        """An orphan project must raise attention in the compact health section."""

        text = self._capture(
            _resources(
                ops_manager_orphans=[
                    "wildcats (Project ID: wildcats-id)"
                ],
            )
        )

        self.assertIn("Status: ATTENTION REQUIRED", text)
        self.assertRegex(text, r"(?m)^Ops Manager orphan projects:\s+1$")
        self.assertNotIn("wildcats-id", text)

    def test_orphan_group_secret_verbose_shows_project_id(self) -> None:
        """Verbose attention output should expose the exact orphan secret evidence."""

        text = self._capture(
            _resources(
                orphan_group_secrets=[
                    "stale-id-group-secret (Project ID: stale-id)"
                ],
            ),
            verbose=True,
        )

        self.assertIn("Status: ATTENTION REQUIRED", text)
        self.assertRegex(text, r"(?m)^Orphan group secrets:\s+1$")
        self.assertIn(
            "stale-id-group-secret (Project ID: stale-id)",
            text,
        )

    def test_missing_platform_project_requires_attention(self) -> None:
        """A missing permanent Ops Manager project remains an attention condition."""

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
        self.assertNotIn("Project ID: NOT FOUND", text)


if __name__ == "__main__":
    unittest.main()
