"""Unit tests for detached asynchronous operation state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from terraform_controller.async_operations import (
    create_operation,
    effective_result,
    load_operation,
    launch_operation,
    mark_failed,
    mark_running,
    mark_succeeded,
    operation_directory,
    submission_instructions,
)
from terraform_controller.common import ControllerError


class AsyncOperationTests(unittest.TestCase):
    """Verify operation state survives independently of the Git working tree."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "checkout" / "terraformController.config"
        self.config_path.parent.mkdir()
        self.config_path.write_text("[dummy]\n", encoding="utf-8")
        self.state_home = self.root / "state"

    def _patch_state_home(self):
        return patch.dict(
            "os.environ",
            {"XDG_STATE_HOME": str(self.state_home)},
            clear=False,
        )

    def test_operation_directory_is_outside_checkout(self) -> None:
        with self._patch_state_home():
            directory = operation_directory(self.config_path)
        self.assertTrue(str(directory).startswith(str(self.state_home)))
        self.assertNotIn(str(self.config_path.parent), str(directory))

    def test_operation_lifecycle_records_positive_result(self) -> None:
        with self._patch_state_home():
            state = create_operation(
                self.config_path,
                command="DeleteShard",
                deployment="SC1",
                worker_arguments=["DeleteShard", "SC1", "1", "--confirm"],
            )
            operation_id = state["operation_id"]
            self.assertEqual(
                load_operation(self.config_path, operation_id)["result"],
                "Queued",
            )

            mark_running(self.config_path, operation_id)
            self.assertEqual(
                effective_result(load_operation(self.config_path, operation_id)),
                "In Progress",
            )

            mark_succeeded(self.config_path, operation_id)
            finished = load_operation(self.config_path, operation_id)
            self.assertEqual(finished["result"], "Succeeded")
            self.assertTrue(finished["completed_at"])

    def test_operation_lifecycle_records_failure_message(self) -> None:
        with self._patch_state_home():
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

    def test_submission_tells_user_exactly_how_to_check_result(self) -> None:
        with self._patch_state_home():
            state = create_operation(
                self.config_path,
                command="AddShard",
                deployment="SC9",
                worker_arguments=["AddShard", "SC9", "2"],
            )
            text = submission_instructions(
                self.config_path,
                state,
                shard_status=True,
            )
        self.assertIn("Check positive/negative result with:", text)
        self.assertIn("ListOperation", text)
        self.assertIn(state["operation_id"], text)
        self.assertIn("ListShards SC9", text)

    def test_duplicate_in_progress_deployment_submission_is_blocked(self) -> None:
        with self._patch_state_home():
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
