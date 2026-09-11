"""Regression tests for Terraform serialization and quiet diagnostic handling."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from privateWorkerReplacement import terraform_runner
from privateWorkerReplacement.common import ControllerError


class TerraformRunnerLockTests(unittest.TestCase):
    """Keep detached workers from racing in one Terraform checkout."""

    def test_apply_inventory_holds_execution_lock_around_transaction(self) -> None:
        """The refresh/init/apply helper must run only inside the lock."""

        with tempfile.TemporaryDirectory() as temp_dir:
            locked = False
            transaction_seen = False

            @contextmanager
            def fake_lock(_config):
                nonlocal locked
                locked = True
                try:
                    yield
                finally:
                    locked = False

            def fake_transaction(_config, _inventory, _operation, _targets=None):
                nonlocal transaction_seen
                self.assertTrue(locked)
                transaction_seen = True

            config = {
                "storage_mode": "dynamic",
                "terraform_cache": Path(temp_dir) / "cache",
            }

            with (
                patch.object(terraform_runner, "_require"),
                patch.object(terraform_runner, "_check_version"),
                patch.object(
                    terraform_runner,
                    "_terraform_execution_lock",
                    fake_lock,
                ),
                patch.object(
                    terraform_runner,
                    "_apply_inventory_locked",
                    fake_transaction,
                ),
            ):
                terraform_runner.apply_inventory(config, {})

            self.assertTrue(transaction_seen)
            self.assertFalse(locked)

    def test_execution_lock_blocks_second_process_until_release(self) -> None:
        """flock must serialize two separate controller worker processes."""

        repo_root = Path(__file__).resolve().parent.parent

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            marker = root / "child-acquired"

            child_code = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from privateWorkerReplacement.terraform_runner import _terraform_execution_lock

cache = Path(sys.argv[2])
marker = Path(sys.argv[3])
with _terraform_execution_lock({"terraform_cache": cache}):
    marker.write_text("acquired", encoding="utf-8")
"""

            with terraform_runner._terraform_execution_lock(
                {"terraform_cache": cache}
            ):
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        child_code,
                        str(repo_root),
                        str(cache),
                        str(marker),
                    ],
                    cwd=repo_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                # Give the child enough time to import the module and attempt
                # the lock. It must still be blocked while the parent holds it.
                time.sleep(0.5)
                self.assertIsNone(child.poll())
                self.assertFalse(marker.exists())

            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(
                child.returncode,
                0,
                msg=f"stdout:\n{stdout}\nstderr:\n{stderr}",
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "acquired")


class TerraformRunnerSyncTests(unittest.TestCase):
    """Verify local Terraform source is copied into a disposable runtime cache."""

    def test_sync_refreshes_cache_from_local_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "terraform-dbaas"
            cache = root / ".runtime" / "terraform-cache"

            source.mkdir(parents=True)
            (source / "main.tf").write_text(
                'terraform { required_version = ">= 1.11.0" }\n',
                encoding="utf-8",
            )
            (source / "nested").mkdir()
            (source / "nested" / "values.tf").write_text(
                'variable "example" {}\n',
                encoding="utf-8",
            )

            cache.mkdir(parents=True)
            (cache / "stale.tf").write_text(
                "stale\n",
                encoding="utf-8",
            )
            (cache / ".terraform").mkdir()
            (cache / ".terraform" / "provider-marker").write_text(
                "keep-me\n",
                encoding="utf-8",
            )

            original_source = (source / "main.tf").read_text(
                encoding="utf-8"
            )

            with patch.object(terraform_runner, "log_event"):
                result = terraform_runner._sync(
                    {
                        "terraform_source": source,
                        "terraform_cache": cache,
                    }
                )

            self.assertEqual(result, cache)
            self.assertTrue((cache / "main.tf").is_file())
            self.assertTrue((cache / "nested" / "values.tf").is_file())
            self.assertFalse((cache / "stale.tf").exists())
            self.assertEqual(
                (cache / ".terraform" / "provider-marker").read_text(
                    encoding="utf-8"
                ),
                "keep-me\n",
            )
            self.assertEqual(
                (source / "main.tf").read_text(encoding="utf-8"),
                original_source,
            )



class TerraformRunnerOutputTests(unittest.TestCase):
    """Keep Git/Terraform implementation chatter off interactive terminals."""

    def test_diagnostic_command_is_captured_and_logged(self) -> None:
        completed = subprocess.CompletedProcess(
            ["terraform", "apply"],
            0,
            stdout="Apply complete!\n",
            stderr="",
        )
        with (
            patch.object(
                terraform_runner,
                "run_process",
                return_value=completed,
            ) as run_mock,
            patch.object(
                terraform_runner,
                "append_process_diagnostic",
                return_value=Path("/tmp/operations.log"),
            ) as log_mock,
        ):
            terraform_runner._run_diagnostic(
                {"config_path": "/tmp/dev.config"},
                ["terraform", "apply"],
                label="Terraform apply",
            )

        self.assertTrue(run_mock.call_args.kwargs["capture"])
        self.assertFalse(run_mock.call_args.kwargs["check"])
        log_mock.assert_called_once()
        self.assertEqual(log_mock.call_args.kwargs["stdout"], "Apply complete!\n")

    def test_diagnostic_failure_is_concise_and_points_to_log(self) -> None:
        completed = subprocess.CompletedProcess(
            ["terraform", "apply"],
            1,
            stdout="very noisy terraform details\n",
            stderr="provider failed\n",
        )
        with (
            patch.object(terraform_runner, "run_process", return_value=completed),
            patch.object(
                terraform_runner,
                "append_process_diagnostic",
                return_value=Path(
                    "/tmp/logs/operations/operations-20260910.log"
                ),
            ),
        ):
            with self.assertRaises(ControllerError) as ctx:
                terraform_runner._run_diagnostic(
                    {"config_path": "/tmp/dev.config"},
                    ["terraform", "apply"],
                    label="Terraform apply",
                )

        message = str(ctx.exception)
        self.assertIn("Terraform apply failed with exit code 1", message)
        self.assertIn("operations-20260910.log", message)
        self.assertNotIn("very noisy terraform details", message)
        self.assertNotIn("provider failed", message)


if __name__ == "__main__":
    unittest.main()
