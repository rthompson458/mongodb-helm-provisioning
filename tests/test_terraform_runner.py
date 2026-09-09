"""Regression tests for terraformController's shared Terraform execution lock."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from terraform_controller import terraform_runner


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
from terraform_controller.terraform_runner import _terraform_execution_lock

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


if __name__ == "__main__":
    unittest.main()
