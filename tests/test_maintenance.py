"""Unit tests for Reconcile safety and convergence orchestration."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from terraform_controller import maintenance

from helpers import FakeVault, deployment_inventory, topology_lock


class MaintenanceTests(unittest.TestCase):
    """Verify Reconcile does not race with active ShardedCluster mutations."""

    def setUp(self) -> None:
        self.config = {
            "rs_ready_timeout": 30,
            "sc_ready_timeout": 30,
        }

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


if __name__ == "__main__":
    unittest.main()
