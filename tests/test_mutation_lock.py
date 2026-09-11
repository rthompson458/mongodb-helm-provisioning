"""Regression tests for controller-wide desired-state mutation serialization."""

from __future__ import annotations

import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from privateWorkerReplacement import mutation_lock


class MutationLockTests(unittest.TestCase):
    """Verify backend scoping and process-lock acquire/release behavior."""

    def _config(self, root: Path) -> dict[str, object]:
        """Return the minimum configuration needed by the mutation lock."""

        return {
            "terraform_cache": root / ".runtime" / "terraform-cache",
            "backend_namespace": "mongodb",
            "backend_secret_suffix": "mongodb-vault-controller",
        }

    def test_lock_path_is_scoped_to_controller_backend(self) -> None:
        """Different controller backends must not accidentally share one lock name."""

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            path = mutation_lock._lock_path(config)

        self.assertEqual(
            path.name,
            ".privateWorkerReplacement-state-mongodb-mongodb-vault-controller.lock",
        )

    def test_mutation_lock_acquires_and_releases_flock(self) -> None:
        """The context manager must hold an exclusive lock for the full body."""

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            with mock.patch.object(mutation_lock.fcntl, "flock") as flock:
                with mutation_lock.controller_state_mutation_lock(
                    config,
                    "AddReplicaSet",
                ):
                    pass

        self.assertEqual(
            flock.call_args_list,
            [
                mock.call(mock.ANY, fcntl.LOCK_EX),
                mock.call(mock.ANY, fcntl.LOCK_UN),
            ],
        )


if __name__ == "__main__":
    unittest.main()
