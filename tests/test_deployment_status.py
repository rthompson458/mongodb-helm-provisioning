"""Unit tests for read-only deployment and shard status presentation."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from privateWorkerReplacement import deployment_status

from helpers import FakeVault, deployment_inventory, online_sc_status


class DeploymentStatusTests(unittest.TestCase):
    """Verify deployment status commands report live state without mutation."""

    def setUp(self) -> None:
        self.config = {
            "rs_ready_timeout": 30,
            "sc_ready_timeout": 30,
        }

    def test_global_list_shards_includes_all_clusters(self) -> None:
        inventory = deployment_inventory(
            deployment_type="ShardedCluster",
            name="SC9",
            shard_count=2,
        )
        inventory.update(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC10",
                shard_count=1,
            )
        )
        vault = FakeVault(inventory)

        def status(_config, key, count):
            return online_sc_status(count, key)

        output = io.StringIO()
        with (
            patch.object(
                deployment_status,
                "read_deployment_lock",
                return_value=None,
            ),
            patch.object(
                deployment_status.kube,
                "sharded_cluster_status",
                side_effect=status,
            ),
            redirect_stdout(output),
        ):
            deployment_status.list_shards(self.config, vault)

        text = output.getvalue()
        self.assertIn("SC9", text)
        self.assertIn("SC10", text)
        self.assertIn("sc9-0", text)
        self.assertIn("sc10-0", text)

    def test_targeted_list_shards_reports_active_change(self) -> None:
        vault = FakeVault(
            deployment_inventory(
                deployment_type="ShardedCluster",
                name="SC9",
                shard_count=3,
            )
        )
        lock = {
            "operation_id": "op-1",
            "category": "topology",
            "action": "AddShard",
            "database": "",
            "start_shards": 3,
            "target_shards": 4,
            "started_at": "2026-09-11T12:00:00Z",
        }
        status = online_sc_status(4, "sc9")
        status["shards"][3].update(
            {"desired": 0, "ready": 0, "updated": 0, "status": "Unknown"}
        )

        output = io.StringIO()
        with (
            patch.object(
                deployment_status,
                "read_deployment_lock",
                return_value=lock,
            ),
            patch.object(deployment_status.kube, "phase", return_value="Running"),
            patch.object(
                deployment_status.kube,
                "sharded_cluster_status",
                return_value=status,
            ),
            redirect_stdout(output),
        ):
            deployment_status.list_shards(self.config, vault, "SC9")

        text = output.getvalue()
        self.assertIn("sc9-3", text)
        self.assertIn("Creating", text)
        self.assertIn("AddShard 3 -> 4", text)
        self.assertIn("2026-09-11T12:00:00Z", text)


if __name__ == "__main__":
    unittest.main()
