"""Unit tests for detached asynchronous operation state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from terraform_controller.async_operations import (
    ASYNC_COMMANDS,
    admin_submission_instructions,
    create_operation,
    effective_result,
    launch_operation,
    load_operation,
    mark_failed,
    mark_running,
    mark_succeeded,
    operation_directory,
    public_submission_instructions,
)
from terraform_controller.common import ControllerError


class AsyncOperationTests(unittest.TestCase):
    """Verify operation state, diagnostics, and customer/admin formatting."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "checkout" / "terraformController.config"
        self.config_path.parent.mkdir()
        self.config_path.write_text("[dummy]\n", encoding="utf-8")

    def test_operation_state_lives_under_logs_tree(self) -> None:
        directory = operation_directory(self.config_path)
        self.assertEqual(
            directory,
            self.config_path.parent / "logs" / "operations" / "state",
        )
        self.assertNotIn(".local", str(directory))

    def test_operation_uses_daily_consolidated_log(self) -> None:
        state = create_operation(
            self.config_path,
            command="AddDatabase",
            deployment="RS1",
            worker_arguments=["AddDatabase", "RS1", "HouseInfo"],
        )
        self.assertRegex(
            Path(state["log_file"]).name,
            r"^operations-\d{8}\.log$",
        )
        self.assertEqual(
            Path(state["log_file"]).parent,
            self.config_path.parent / "logs" / "operations",
        )

    def test_database_create_and_delete_are_async_commands(self) -> None:
        self.assertIn("AddDatabase", ASYNC_COMMANDS)
        self.assertIn("DeleteDatabase", ASYNC_COMMANDS)

    def test_operation_lifecycle_records_positive_result(self) -> None:
        state = create_operation(
            self.config_path,
            command="DeleteShard",
            deployment="SC1",
            worker_arguments=["DeleteShard", "SC1", "1", "--confirm"],
        )
        operation_id = state["operation_id"]
        self.assertEqual(load_operation(self.config_path, operation_id)["result"], "Queued")

        mark_running(self.config_path, operation_id)
        self.assertEqual(
            effective_result(load_operation(self.config_path, operation_id)),
            "In Progress",
        )

        mark_succeeded(self.config_path, operation_id)
        finished = load_operation(self.config_path, operation_id)
        self.assertEqual(finished["result"], "Succeeded")
        self.assertTrue(finished["completed_at"])
        self.assertTrue(finished["transcript_archived"])
        self.assertTrue(Path(finished["log_file"]).exists())

    def test_operation_lifecycle_records_failure_message(self) -> None:
        state = create_operation(
            self.config_path,
            command="DeleteShard",
            deployment="SC1",
            worker_arguments=["DeleteShard", "SC1", "5", "--confirm"],
        )
        mark_failed(
            self.config_path,
            state["operation_id"],
            "must retain at least 1 shard",
        )
        failed = load_operation(self.config_path, state["operation_id"])
        self.assertEqual(failed["result"], "Failed")
        self.assertIn("retain at least 1 shard", failed["message"])

    def test_public_submission_hides_operation_internals(self) -> None:
        state = create_operation(
            self.config_path,
            command="AddShard",
            deployment="SC9",
            worker_arguments=["AddShard", "SC9", "2"],
        )
        text = public_submission_instructions(
            self.config_path,
            state,
            details=[("ShardedCluster", "SC9")],
            status_text="Topology change requested",
            status_arguments=["ListShards", "SC9"],
        )
        self.assertIn("AddShard request accepted.", text)
        self.assertIn("ShardedCluster: SC9", text)
        self.assertIn("python3 terraformController.py", text)
        self.assertIn("ListShards SC9", text)
        self.assertNotIn("/usr/bin/python3", text)
        self.assertNotIn("ListOperation", text)
        self.assertNotIn(state["operation_id"], text)
        self.assertNotIn("Worker PID", text)

    def test_public_database_submission_shows_database_fields(self) -> None:
        state = create_operation(
            self.config_path,
            command="AddDatabase",
            deployment="RS1",
            worker_arguments=["AddDatabase", "RS1", "HouseInfo"],
        )
        text = public_submission_instructions(
            self.config_path,
            state,
            details=[("Deployment", "RS1"), ("Database", "HouseInfo")],
            status_text="Creation requested",
            status_arguments=["ListDatabase", "RS1", "HouseInfo"],
        )
        self.assertIn("Deployment:     RS1", text)
        self.assertIn("Database:       HouseInfo", text)
        self.assertIn("ListDatabase RS1 HouseInfo", text)

    def test_admin_submission_exposes_exact_operation_diagnostics(self) -> None:
        state = create_operation(
            self.config_path,
            command="RecoverOrphanedResources",
            deployment="controller-state",
            worker_arguments=["RecoverOrphanedResources", "--confirm"],
        )
        text = admin_submission_instructions(self.config_path, state)
        self.assertIn("Operation ID:", text)
        self.assertIn(state["operation_id"], text)
        self.assertIn("python3 terraformControllerAdmin.py", text)
        self.assertNotIn("/usr/bin/python3", text)
        self.assertIn("ListOperation", text)

    def test_duplicate_in_progress_deployment_submission_is_blocked(self) -> None:
        state = create_operation(
            self.config_path,
            command="AddShard",
            deployment="SC9",
            worker_arguments=["AddShard", "SC9", "1"],
        )
        mark_running(self.config_path, state["operation_id"])
        with self.assertRaises(ControllerError):
            launch_operation(
                self.config_path,
                self.root,
                command="DeleteShard",
                deployment="SC9",
                worker_arguments=["DeleteShard", "SC9", "1", "--confirm"],
            )


if __name__ == "__main__":
    unittest.main()
